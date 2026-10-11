#!/usr/bin/env python3
"""Add GPS locations to Google Photos Takeout images using a Google Maps Timeline export.

Inputs:
  * Timeline.json exported from an Android phone
    (Settings > Location > Location services > Timeline > Export Timeline data).
    Older Takeout formats (Records.json, iOS-style segment lists) are also read.
  * A folder extracted from a Google Takeout export of Google Photos
    (media files plus their .json sidecar files).

Output:
  * Geotagged copies of photos that had no location, written to --out.
    Originals are never modified.
  * A CSV report describing what happened to every photo.

Requires ExifTool (https://exiftool.org) on PATH. Python 3.8+, standard library only.
"""

import argparse
import bisect
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Formats where ExifTool writes standard EXIF GPS tags that Google Photos reads.
IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".dng"}

_FLOAT_PAIR = re.compile(r"(-?\d+(?:\.\d+)?)\s*°?\s*,\s*(-?\d+(?:\.\d+)?)")
_DUP_SUFFIX = re.compile(r"^(.*)\((\d+)\)$")
_SUPPLEMENTAL = ".supplemental-metadata"


# --------------------------------------------------------------------------- timeline

def parse_latlng(value):
    """Return (lat, lng) from '51.5°, -0.12°', 'geo:51.5,-0.12' or an E7 dict."""
    if isinstance(value, dict):
        for key in ("latLng", "LatLng", "point"):
            if key in value:
                return parse_latlng(value[key])
        if "latitudeE7" in value and "longitudeE7" in value:
            return value["latitudeE7"] / 1e7, value["longitudeE7"] / 1e7
        return None
    if not isinstance(value, str):
        return None
    m = _FLOAT_PAIR.search(value)
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return lat, lng


def parse_time(value):
    """Parse an ISO-8601 timestamp (or epoch milliseconds) into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    s = value.strip().replace("Z", "+00:00")
    # fromisoformat before 3.11 only accepts 0, 3 or 6 fractional digits.
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:
        frac = (m.group(2) or "")[1:7].ljust(6, "0") if m.group(2) else ""
        s = m.group(1) + ("." + frac if frac else "") + m.group(3)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class _Intervals:
    """Sorted (start, end, ...) tuples with a fast 'which intervals contain t' lookup."""

    def __init__(self, items):
        self.items = sorted(items, key=lambda x: x[0])
        self.starts = [x[0] for x in self.items]
        self.max_len = max((x[1] - x[0] for x in self.items), default=timedelta(0))

    def containing(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        while i >= 0 and self.starts[i] >= t - self.max_len:
            if self.items[i][1] >= t:
                yield self.items[i]
            i -= 1


class Timeline:
    def __init__(self):
        self.visits = []    # (start, end, lat, lng, place_id, utc_offset_minutes)
        self.points = []    # (time, lat, lng, source)
        self.offsets = []   # (start, end, utc_offset_minutes) from segments
        self.shift = timedelta(0)  # added to every Timeline timestamp (see --time-shift)

    def finalize(self):
        self.points.sort(key=lambda p: p[0])
        self._times = [p[0] for p in self.points]
        self._visits = _Intervals(self.visits)
        self._offsets = _Intervals(self.offsets)
        times = self._times + [v[0] for v in self.visits] + [v[1] for v in self.visits]
        self.start = min(times) if times else None
        self.end = max(times) if times else None

    def utc_offset_minutes(self, t):
        for item in self._offsets.containing(t - self.shift):
            return item[2]
        return None

    def locate(self, t, max_gap):
        """Return (lat, lng, method, gap_minutes, place_id) for UTC time t, or None."""
        t = t - self.shift
        # 1. Inside a visit: use the shortest (most specific) visit containing t.
        best = min(self._visits.containing(t), key=lambda v: v[1] - v[0], default=None)
        if best:
            return best[2], best[3], "visit", 0.0, best[4]

        # 2. Between path/raw points: interpolate if both neighbours are close, else nearest.
        j = bisect.bisect_left(self._times, t)
        before = self.points[j - 1] if j > 0 else None
        after = self.points[j] if j < len(self.points) else None
        gap_b = (t - before[0]) if before else None
        gap_a = (after[0] - t) if after else None
        if before and after and gap_b <= max_gap and gap_a <= max_gap:
            span = (after[0] - before[0]).total_seconds()
            f = 0.0 if span == 0 else gap_b.total_seconds() / span
            lat = before[1] + (after[1] - before[1]) * f
            lng = before[2] + (after[2] - before[2]) * f
            gap = max(gap_b, gap_a).total_seconds() / 60
            return lat, lng, "interpolated", round(gap, 1), ""
        candidates = [(g, p) for g, p in ((gap_b, before), (gap_a, after)) if p and g <= max_gap]
        if candidates:
            g, p = min(candidates, key=lambda c: c[0])
            return p[1], p[2], "nearest:" + p[3], round(g.total_seconds() / 60, 1), ""
        return None


def _add_segment(tl, seg):
    start = parse_time(seg.get("startTime"))
    end = parse_time(seg.get("endTime"))
    offset = seg.get("startTimeTimezoneUtcOffsetMinutes")
    if start and end and isinstance(offset, int):
        tl.offsets.append((start, end, offset))

    visit = seg.get("visit")
    if visit and start and end:
        cand = visit.get("topCandidate") or {}
        loc = parse_latlng(cand.get("placeLocation"))
        if loc:
            tl.visits.append((start, end, loc[0], loc[1], cand.get("placeId", ""), offset))

    activity = seg.get("activity")
    if activity:
        s = parse_latlng(activity.get("start"))
        e = parse_latlng(activity.get("end"))
        if s and start:
            tl.points.append((start, s[0], s[1], "activity"))
        if e and end:
            tl.points.append((end, e[0], e[1], "activity"))

    path = seg.get("timelinePath")
    if isinstance(path, list) and path:
        n = len(path)
        for k, p in enumerate(path):
            loc = parse_latlng(p.get("point") if isinstance(p, dict) else p)
            if not loc:
                continue
            t = parse_time(p.get("time")) if isinstance(p, dict) else None
            if t is None and isinstance(p, dict) and start and "durationMinutesOffsetFromStartTime" in p:
                t = start + timedelta(minutes=float(p["durationMinutesOffsetFromStartTime"]))
            if t is None and start and end:
                t = start + (end - start) * (k / max(n - 1, 1))
            if t:
                tl.points.append((t, loc[0], loc[1], "path"))


def load_timeline(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    tl = Timeline()
    if isinstance(data, list):                      # iOS-style export: list of segments
        segments, raw, records = data, [], []
    else:
        segments = data.get("semanticSegments", [])
        raw = data.get("rawSignals", [])
        records = data.get("locations", [])         # legacy Takeout Records.json
    for seg in segments:
        if isinstance(seg, dict):
            _add_segment(tl, seg)
    for sig in raw:
        pos = sig.get("position") if isinstance(sig, dict) else None
        if pos:
            loc = parse_latlng(pos)
            t = parse_time(pos.get("timestamp"))
            if loc and t:
                tl.points.append((t, loc[0], loc[1], "raw"))
    for rec in records:
        loc = parse_latlng(rec)
        t = parse_time(rec.get("timestamp") or rec.get("timestampMs"))
        if loc and t:
            tl.points.append((t, loc[0], loc[1], "records"))
    tl.finalize()
    return tl


# --------------------------------------------------------------------------- takeout

def _sidecar_key(json_name):
    """'IMG_1.jpg.supplemental-metadata(1).json' -> ('IMG_1.jpg', '1')."""
    base = json_name[: -len(".json")]
    dup = ""
    m = _DUP_SUFFIX.match(base)
    if m:
        base, dup = m.group(1), m.group(2)
    # Takeout truncates long names, so '.supplemental-metadata' may appear cut short.
    dot = base.rfind(".")
    if dot > 0 and len(base) - dot > 1 and _SUPPLEMENTAL.startswith(base[dot:]) and len(base) - dot >= 3:
        base = base[:dot]
    return base, dup


def _media_key(name):
    """'IMG_1(1).jpg' -> ('IMG_1.jpg', '1'); strips '-edited' to share the original's sidecar."""
    stem, ext = os.path.splitext(name)
    dup = ""
    m = _DUP_SUFFIX.match(stem)
    if m:
        stem, dup = m.group(1), m.group(2)
    if stem.endswith("-edited"):
        stem = stem[: -len("-edited")]
    return stem + ext, dup


def find_sidecar(media_name, sidecars):
    """sidecars: {(base, dup): json_path} for one directory."""
    key, dup = _media_key(media_name)
    if (key, dup) in sidecars:
        return sidecars[(key, dup)]
    lower = {(b.lower(), d): p for (b, d), p in sidecars.items()}
    if (key.lower(), dup) in lower:
        return lower[(key.lower(), dup)]
    # Truncated sidecar name: unique sidecar whose base is a long prefix of the media name.
    matches = [p for (b, d), p in sidecars.items() if d == dup and len(b) >= 30 and key.startswith(b)]
    return matches[0] if len(matches) == 1 else None


def read_sidecar(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, None
    taken = None
    ts = (data.get("photoTakenTime") or {}).get("timestamp")
    if ts and str(ts).isdigit() and int(ts) > 0:
        taken = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    has_geo = False
    for key in ("geoDataExif", "geoData"):
        g = data.get(key) or {}
        if g.get("latitude") or g.get("longitude"):
            has_geo = True
    return taken, has_geo


# --------------------------------------------------------------------------- progress

class Progress:
    """Single-line 'label: done/total (pct%)' counter; prints a new line every 10% when not a terminal."""

    def __init__(self, label, total):
        self.label, self.total, self.done = label, total, 0
        self.tty = sys.stdout.isatty()
        self._last_pct = -1
        self._show()

    def advance(self, n=1):
        self.done = min(self.total, self.done + n)
        self._show()

    def _show(self):
        pct = 100 * self.done // self.total if self.total else 100
        if self.tty:
            print("\r%s: %d/%d (%d%%)" % (self.label, self.done, self.total, pct), end="", flush=True)
        elif pct // 10 != self._last_pct // 10 or self.done == self.total:
            print("%s: %d/%d (%d%%)" % (self.label, self.done, self.total, pct), flush=True)
        self._last_pct = pct

    def finish(self):
        if self.tty:
            print()


# --------------------------------------------------------------------------- exiftool

def _exiftool_cmd(exiftool, args):
    """Run exiftool with arguments passed via a UTF-8 argfile (safe for non-ASCII paths on Windows)."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".args", delete=False) as af:
        af.write("\n".join(args) + "\n")
        argfile = af.name
    try:
        return subprocess.run([exiftool, "-charset", "filename=utf8", "-@", argfile],
                              capture_output=True, check=False)
    finally:
        os.unlink(argfile)


def read_exif(exiftool, files, batch=100):
    """Return {abs_path: {tag: value}} for GPS and capture-time tags."""
    out = {}
    progress = Progress("Reading photo metadata", len(files))
    for i in range(0, len(files), batch):
        chunk = files[i:i + batch]
        args = ["-json", "-n", "-GPSLatitude", "-GPSLongitude", "-DateTimeOriginal",
                "-OffsetTimeOriginal", "-charset", "exif=utf8"] + chunk
        res = _exiftool_cmd(exiftool, args)
        try:
            rows = json.loads(res.stdout.decode("utf-8") or "[]")
        except ValueError:
            rows = []
        for row in rows:
            out[os.path.normcase(os.path.abspath(row.get("SourceFile", "")))] = row
        progress.advance(len(chunk))
    progress.finish()
    return out


def exif_time(row):
    """DateTimeOriginal + OffsetTimeOriginal -> UTC datetime, or None if offset unknown."""
    dto, off = row.get("DateTimeOriginal"), row.get("OffsetTimeOriginal")
    if not dto or not off:
        return None
    try:
        local = datetime.strptime(str(dto)[:19], "%Y:%m:%d %H:%M:%S")
        sign = -1 if str(off).startswith("-") else 1
        hh, mm = str(off).lstrip("+-").split(":")[:2]
        tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
        return local.replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return None


def write_tags(exiftool, jobs, batch=50):
    """jobs: list of dicts with dest, lat, lng, and optional set_date (UTC) and utc_offset (minutes).

    Returns the set of dest paths ExifTool reported errors for."""
    failed = set()
    progress = Progress("Writing locations", len(jobs))
    for b in range(0, len(jobs), batch):
        args = []
        for job in jobs[b:b + batch]:
            lat, lng = job["lat"], job["lng"]
            args += ["-overwrite_original", "-P",
                     "-GPSLatitude=%.7f" % abs(lat), "-GPSLatitudeRef=" + ("N" if lat >= 0 else "S"),
                     "-GPSLongitude=%.7f" % abs(lng), "-GPSLongitudeRef=" + ("E" if lng >= 0 else "W")]
            if job.get("set_date"):
                # Photo had no DateTimeOriginal; add one so Google Photos doesn't use the upload date.
                off = job.get("utc_offset") or 0
                local = job["set_date"] + timedelta(minutes=off)
                off_str = "%s%02d:%02d" % ("-" if off < 0 else "+", abs(off) // 60, abs(off) % 60)
                args += ["-DateTimeOriginal=" + local.strftime("%Y:%m:%d %H:%M:%S"),
                         "-OffsetTimeOriginal=" + off_str]
            args += [job["dest"], "-execute"]
        res = _exiftool_cmd(exiftool, args)
        err = res.stderr.decode("utf-8", "replace")
        if err:
            sys.stderr.write("\n" + err)
        for job in jobs[b:b + batch]:
            if job["dest"] in err:
                failed.add(job["dest"])
        progress.advance(len(jobs[b:b + batch]))
    progress.finish()
    return failed


# --------------------------------------------------------------------------- calibration

def distance_km(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, h)))


MIN_CALIBRATION_PHOTOS = 20


def calibrate(tl, known, max_gap, shifts_hours=None, max_samples=1000):
    """Score candidate time shifts using photos whose camera already recorded GPS.

    known: list of (taken_utc, lat, lng[, file]). Only photos taken within the Timeline's date range
    are used: shifting photos from outside it into it would reward large, meaningless shifts.
    Returns ([(shift_hours, matched, within_1km, median_km)] best first, photos used, photos left out)."""
    if shifts_hours is None:
        shifts_hours = [h / 2 for h in range(-28, 29)]   # -14h .. +14h in 30-minute steps
    inside = [k for k in known if tl.start and tl.start <= k[0] <= tl.end]
    step = max(1, len(inside) // max_samples)
    sample = inside[::step]
    original = tl.shift
    results = []
    progress = Progress("Checking time shifts", len(shifts_hours))
    try:
        for h in shifts_hours:
            tl.shift = timedelta(hours=h)
            dists = []
            for k in sample:
                hit = tl.locate(k[0], max_gap)
                if hit:
                    dists.append(distance_km(k[1], k[2], hit[0], hit[1]))
            dists.sort()
            near = sum(1 for d in dists if d <= 1.0)
            median = dists[len(dists) // 2] if dists else float("nan")
            results.append((h, len(dists), near, median))
            progress.advance()
    finally:
        progress.finish()
        tl.shift = original
    results.sort(key=lambda r: (-r[2], r[3] if r[1] else float("inf"), abs(r[0])))
    return results, len(sample), len(known) - len(inside)


def print_calibration(results, n, left_out, current_shift):
    """Print the table; return a better shift to suggest, or None."""
    print("\nTime-shift check using %d photos that have camera GPS and fall within the Timeline's dates"
          % n + (" (%d others are outside those dates and were not used)" % left_out if left_out else "") + ":")
    print("  shift(h)  matched  within 1 km  median km")
    current = next((r for r in results if r[0] == current_shift), None)
    for r in results[:5] + ([current] if current and current not in results[:5] else []):
        mark = "  <- current" if r[0] == current_shift else ""
        print("  %+7.1f  %7d  %11d  %9.2f%s" % (r[0], r[1], r[2], r[3], mark))
    if n < MIN_CALIBRATION_PHOTOS:
        print("Inconclusive: fewer than %d photos to compare. Check some report rows by hand instead."
              % MIN_CALIBRATION_PHOTOS)
        return None
    best = results[0]
    if current and best[0] != current_shift and best[2] > current[2] * 1.2 + 5 and best[2] >= best[1] / 2:
        return best[0]
    return None


def write_calibration_detail(path, tl, known, max_gap):
    """Per-photo comparison at the current shift, for checking the check."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "taken_utc", "in_timeline_dates", "photo_lat", "photo_lng",
                    "timeline_lat", "timeline_lng", "method", "gap_minutes", "distance_km"])
        for k in sorted(known, key=lambda k: k[0]):
            taken, lat, lng = k[:3]
            inside = bool(tl.start and tl.start <= taken <= tl.end)
            hit = tl.locate(taken, max_gap)
            row = [k[3] if len(k) > 3 else "", taken.isoformat(), inside, "%.6f" % lat, "%.6f" % lng]
            if hit:
                row += ["%.6f" % hit[0], "%.6f" % hit[1], hit[2], hit[3],
                        "%.2f" % distance_km(lat, lng, hit[0], hit[1])]
            else:
                row += ["", "", "no_match", "", ""]
            w.writerow(row)


# --------------------------------------------------------------------------- main

def collect_media(root):
    media = []
    for dirpath, _, filenames in os.walk(root):
        sidecars = {}
        for name in filenames:
            if name.lower().endswith(".json"):
                sidecars[_sidecar_key(name)] = os.path.join(dirpath, name)
        for name in sorted(filenames):
            if name.lower().endswith(".json"):
                continue
            media.append((os.path.join(dirpath, name), find_sidecar(name, sidecars)))
    return media


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--timeline", required=True, help="Timeline.json exported from your phone")
    ap.add_argument("--takeout", required=True, help="extracted Google Photos Takeout folder")
    ap.add_argument("--out", required=True, help="folder for geotagged copies (must not be inside --takeout)")
    ap.add_argument("--report", help="CSV report path (default: <out>/geotag_report.csv)")
    ap.add_argument("--max-gap", type=float, default=30,
                    help="max minutes between photo and a timeline point outside visits (default 30)")
    ap.add_argument("--dry-run", action="store_true", help="write the report only; copy nothing")
    ap.add_argument("--time-shift", type=float, default=0,
                    help="hours to add to every Timeline timestamp, if the check shows they're offset (default 0)")
    ap.add_argument("--ignore-shift-check", action="store_true",
                    help="write files even if the time-shift check suggests a different --time-shift")
    ap.add_argument("--exiftool", default=shutil.which("exiftool") or "exiftool", help="path to exiftool")
    a = ap.parse_args(argv)

    takeout = os.path.abspath(a.takeout)
    out = os.path.abspath(a.out)
    if out == takeout or out.startswith(takeout + os.sep):
        ap.error("--out must not be inside --takeout")
    if not a.dry_run and shutil.which(a.exiftool) is None and not os.path.isfile(a.exiftool):
        ap.error("exiftool not found; install it from https://exiftool.org or pass --exiftool")

    print("Loading Timeline...", flush=True)
    tl = load_timeline(a.timeline)
    tl.shift = timedelta(hours=a.time_shift)
    print("Timeline: %d visits, %d location points" % (len(tl.visits), len(tl.points)))
    if tl.points or tl.visits:
        all_t = [p[0] for p in tl.points] + [v[0] for v in tl.visits]
        print("Timeline covers %s to %s (UTC)" % (min(all_t).date(), max(all_t).date()))

    print("Scanning Takeout folder...", flush=True)
    media = collect_media(takeout)
    print("Found %d files (%d photos of a supported type)" % (
        len(media), sum(1 for p, _ in media if os.path.splitext(p)[1].lower() in IMAGE_EXTS)))
    images = [(p, s) for p, s in media if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
    have_exiftool = shutil.which(a.exiftool) is not None or os.path.isfile(a.exiftool)
    if not have_exiftool:
        print("Warning: exiftool not found; existing EXIF GPS can't be detected in dry run")
    exif = read_exif(a.exiftool, [p for p, _ in images]) if have_exiftool else {}

    max_gap = timedelta(minutes=a.max_gap)
    rows, jobs, known = [], [], []
    for path, sidecar in media:
        rel = os.path.relpath(path, takeout)
        row = {"file": rel, "status": "", "taken_utc": "", "time_source": "", "lat": "", "lng": "",
               "method": "", "gap_minutes": "", "place_id": "", "output": ""}
        rows.append(row)
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
            row["status"] = "skipped_unsupported_type"
            continue
        ex = exif.get(os.path.normcase(os.path.abspath(path)), {})
        side_time, side_geo = read_sidecar(sidecar) if sidecar else (None, False)
        taken, row["time_source"] = side_time, "sidecar"
        if taken is None:
            taken, row["time_source"] = exif_time(ex), "exif"
        if ex.get("GPSLatitude") or ex.get("GPSLongitude") or side_geo:
            row["status"] = "skipped_has_location"
            if taken and isinstance(ex.get("GPSLatitude"), (int, float)) \
                    and isinstance(ex.get("GPSLongitude"), (int, float)):
                known.append((taken, ex["GPSLatitude"], ex["GPSLongitude"], rel))
            continue
        if taken is None:
            row["status"], row["time_source"] = "skipped_no_reliable_time", ""
            continue
        row["taken_utc"] = taken.isoformat()
        hit = tl.locate(taken, max_gap)
        if not hit:
            row["status"] = "no_timeline_match"
            continue
        lat, lng, method, gap, place = hit
        row.update(lat="%.6f" % lat, lng="%.6f" % lng, method=method, gap_minutes=gap, place_id=place)
        dest = os.path.join(out, rel)
        row["output"] = os.path.relpath(dest, out)
        row["status"] = "would_tag" if a.dry_run else "tagged"
        jobs.append({"src": path, "dest": dest, "lat": lat, "lng": lng, "row": row,
                     "set_date": None if ex.get("DateTimeOriginal") else taken,
                     "utc_offset": tl.utc_offset_minutes(taken)})

    report = a.report or os.path.join(out, "geotag_report.csv")
    os.makedirs(os.path.dirname(os.path.abspath(report)), exist_ok=True)
    suggested = None
    if known:
        results, n, left_out = calibrate(tl, known, max_gap)
        suggested = print_calibration(results, n, left_out, a.time_shift)
        detail = os.path.join(os.path.dirname(os.path.abspath(report)), "time_shift_check.csv")
        write_calibration_detail(detail, tl, known, max_gap)
        print("Per-photo detail: %s" % detail)
    else:
        print("\nTime-shift check skipped: no photos with camera GPS to compare against.")
    if suggested is not None:
        print("WARNING: --time-shift %g matches your geotagged photos better than the current %g."
              % (suggested, a.time_shift))
        if not a.dry_run and not a.ignore_shift_check:
            print("Nothing copied. Rerun with --time-shift %g, or add --ignore-shift-check." % suggested)
            a.dry_run = True
            for job in jobs:
                job["row"]["status"] = "would_tag"

    if not a.dry_run and jobs:
        progress = Progress("Copying photos", len(jobs))
        for job in jobs:
            os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
            shutil.copy2(job["src"], job["dest"])
            progress.advance()
        progress.finish()
        failed = write_tags(a.exiftool, jobs)
        for job in jobs:
            if job["dest"] in failed:
                job["row"]["status"] = "write_failed"

    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for k in sorted(counts):
        print("%-28s %d" % (k, counts[k]))
    print("Report: %s" % report)
    return 0


if __name__ == "__main__":
    sys.exit(run())
