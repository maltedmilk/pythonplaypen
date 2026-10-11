import base64
import csv
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import geotag_from_timeline as g

# 8x8 JPEG with no EXIF.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19i"
    "Z2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2NjY2NjY2P/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUF"
    "BAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW"
    "V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi"
    "4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC"
    "AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVm"
    "Z2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq"
    "8vP09fb3+Pn6/9oADAMBAAIRAxEAPwBtFFFeWeyf/9k="
)

TIMELINE = {
    "semanticSegments": [
        {   # Visit 10:00-11:00 BST at a place in London.
            "startTime": "2024-06-01T10:00:00.000+01:00", "endTime": "2024-06-01T11:00:00.000+01:00",
            "startTimeTimezoneUtcOffsetMinutes": 60,
            "visit": {"hierarchyLevel": 0, "probability": 0.9,
                      "topCandidate": {"placeId": "PLACE_A", "placeLocation": {"latLng": "51.5000000°, -0.1200000°"}}},
        },
        {   # Path 11:00-11:20 BST.
            "startTime": "2024-06-01T11:00:00.000+01:00", "endTime": "2024-06-01T11:20:00.000+01:00",
            "timelinePath": [
                {"point": "51.5000000°, -0.1200000°", "time": "2024-06-01T11:00:00.000+01:00"},
                {"point": "51.6000000°, -0.2200000°", "time": "2024-06-01T11:20:00.000+01:00"},
            ],
        },
    ],
    "rawSignals": [
        {"position": {"LatLng": "48.8566000°, 2.3522000°", "accuracyMeters": 10,
                      "timestamp": "2024-06-02T09:00:00.000+02:00"}},
    ],
}


def epoch(iso):
    return str(int(datetime.fromisoformat(iso).timestamp()))


def sidecar(title, taken_iso, lat=0.0, lng=0.0):
    return {"title": title, "photoTakenTime": {"timestamp": epoch(taken_iso)},
            "geoData": {"latitude": lat, "longitude": lng, "altitude": 0.0}}


class ParsingTests(unittest.TestCase):
    def test_parse_latlng_formats(self):
        self.assertEqual(g.parse_latlng("51.5°, -0.12°"), (51.5, -0.12))
        self.assertEqual(g.parse_latlng("geo:51.5,-0.12"), (51.5, -0.12))
        self.assertEqual(g.parse_latlng({"latitudeE7": 515000000, "longitudeE7": -1200000}), (51.5, -0.12))
        self.assertIsNone(g.parse_latlng("0°, 0°"))

    def test_parse_time_fraction_digits(self):
        t = g.parse_time("2024-06-01T10:00:00.12+01:00")
        self.assertEqual(t, datetime(2024, 6, 1, 9, 0, 0, 120000, tzinfo=timezone.utc))

    def test_sidecar_name_matching(self):
        names = ["IMG_1.jpg.json", "IMG_1.jpg(1).json", "IMG_2.jpg.supplemental-metadata.json",
                 "IMG_3.jpg.supplemental-metad.json",
                 "Screenshot_20240601-100000_Some Long App Na.json"]
        sc = {g._sidecar_key(n): n for n in names}
        self.assertEqual(g.find_sidecar("IMG_1.jpg", sc), "IMG_1.jpg.json")
        self.assertEqual(g.find_sidecar("IMG_1(1).jpg", sc), "IMG_1.jpg(1).json")
        self.assertEqual(g.find_sidecar("IMG_1-edited.jpg", sc), "IMG_1.jpg.json")
        self.assertEqual(g.find_sidecar("IMG_2.jpg", sc), "IMG_2.jpg.supplemental-metadata.json")
        self.assertEqual(g.find_sidecar("IMG_3.jpg", sc), "IMG_3.jpg.supplemental-metad.json")
        self.assertEqual(g.find_sidecar("Screenshot_20240601-100000_Some Long App Name.jpg", sc),
                         "Screenshot_20240601-100000_Some Long App Na.json")
        self.assertIsNone(g.find_sidecar("IMG_9.jpg", sc))


class LocateTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(TIMELINE, f)
        self.tl = g.load_timeline(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_visit(self):
        hit = self.tl.locate(g.parse_time("2024-06-01T10:30:00+01:00"), timedelta(minutes=30))
        self.assertEqual(hit[:3], (51.5, -0.12, "visit"))
        self.assertEqual(hit[4], "PLACE_A")

    def test_interpolated(self):
        lat, lng, method, gap, _ = self.tl.locate(g.parse_time("2024-06-01T11:10:00+01:00"), timedelta(minutes=30))
        self.assertEqual(method, "interpolated")
        self.assertAlmostEqual(lat, 51.55)
        self.assertAlmostEqual(lng, -0.17)

    def test_nearest_raw_and_gap_limit(self):
        hit = self.tl.locate(g.parse_time("2024-06-02T09:10:00+02:00"), timedelta(minutes=30))
        self.assertEqual(hit[2], "nearest:raw")
        self.assertIsNone(self.tl.locate(g.parse_time("2024-06-02T10:00:00+02:00"), timedelta(minutes=30)))

    def test_utc_offset(self):
        self.assertEqual(self.tl.utc_offset_minutes(g.parse_time("2024-06-01T10:30:00+01:00")), 60)


def moving_timeline(label_offset_hours=0):
    """A day of hourly path points moving 0.1 degrees east per hour from (51.5, -3.0).

    label_offset_hours > 0 simulates UK-local times wrongly labelled as +HH:00."""
    start = datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc)
    tz = timezone(timedelta(hours=label_offset_hours))
    path = []
    for h in range(24):
        true_t = start + timedelta(hours=h)
        labelled = true_t.replace(tzinfo=None).replace(tzinfo=tz)  # same clock time, wrong offset
        path.append({"point": "51.5°, %.1f°" % (-3.0 + 0.1 * h), "time": labelled.isoformat()})
    return {"semanticSegments": [{"startTime": path[0]["time"], "endTime": path[-1]["time"],
                                  "timelinePath": path}]}


def known_photos():
    """(taken_utc, lat, lng) for photos taken on the hour along moving_timeline's true route."""
    start = datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc)
    return [(start + timedelta(hours=h / 2), 51.5, -3.0 + 0.05 * h) for h in range(8, 39)]


class CalibrationTests(unittest.TestCase):
    def load(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        self.addCleanup(os.unlink, path)
        return g.load_timeline(path)

    def test_correct_timeline_prefers_zero(self):
        results, n, _ = g.calibrate(self.load(moving_timeline(0)), known_photos(), timedelta(minutes=30))
        self.assertEqual(results[0][0], 0)
        self.assertEqual(results[0][2], n)

    def test_mislabelled_timeline_detects_shift(self):
        tl = self.load(moving_timeline(3))
        results, _, _ = g.calibrate(tl, known_photos(), timedelta(minutes=30))
        self.assertEqual(results[0][0], 3)
        self.assertEqual(tl.shift, timedelta(0))  # calibrate restores the shift

    def test_photos_after_timeline_end_are_ignored(self):
        # Regression: photos taken after the export near the last Timeline location made a
        # +12h shift look best, because shifting pulled them into the Timeline's final hours.
        tl = self.load(moving_timeline(0))
        end = datetime(2024, 6, 1, 23, 0, tzinfo=timezone.utc)
        after = [(end + timedelta(hours=10, minutes=m), 51.5, -0.7) for m in range(0, 120, 2)]
        results, n, left_out = g.calibrate(tl, after + known_photos()[:3], timedelta(minutes=30))
        self.assertEqual((n, left_out), (3, 60))
        self.assertIsNone(g.print_calibration(results, n, left_out, 0))

    def test_time_shift_applied_to_lookup(self):
        tl = self.load(moving_timeline(3))
        taken, lat, lng = known_photos()[0]
        tl.shift = timedelta(hours=3)
        hit = tl.locate(taken, timedelta(minutes=30))
        self.assertAlmostEqual(hit[1], lng)


@unittest.skipUnless(shutil.which("exiftool"), "exiftool not installed")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.takeout = os.path.join(self.tmp, "Takeout", "Google Photos", "Photos from 2024")
        os.makedirs(self.takeout)
        self.out = os.path.join(self.tmp, "out")
        self.timeline = os.path.join(self.tmp, "Timeline.json")
        with open(self.timeline, "w") as f:
            json.dump(TIMELINE, f)

        def add(name, sc_name, sc):
            with open(os.path.join(self.takeout, name), "wb") as f:
                f.write(TINY_JPEG)
            if sc_name:
                with open(os.path.join(self.takeout, sc_name), "w") as f:
                    json.dump(sc, f)

        add("in_visit.jpg", "in_visit.jpg.supplemental-metadata.json",
            sidecar("in_visit.jpg", "2024-06-01T10:30:00+01:00"))
        add("has_geo.jpg", "has_geo.jpg.json", sidecar("has_geo.jpg", "2024-06-01T10:30:00+01:00", 40.0, -3.0))
        add("no_match.jpg", "no_match.jpg.json", sidecar("no_match.jpg", "2023-01-01T12:00:00+00:00"))
        add("no_sidecar.jpg", None, None)
        with open(os.path.join(self.takeout, "clip.mp4"), "wb") as f:
            f.write(b"\0" * 16)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def report(self):
        with open(os.path.join(self.out, "geotag_report.csv"), newline="") as f:
            return {os.path.basename(r["file"]): r for r in csv.DictReader(f)}

    def test_run(self):
        g.run(["--timeline", self.timeline, "--takeout", os.path.join(self.tmp, "Takeout"), "--out", self.out])
        r = self.report()
        self.assertEqual(r["in_visit.jpg"]["status"], "tagged")
        self.assertEqual(r["has_geo.jpg"]["status"], "skipped_has_location")
        self.assertEqual(r["no_match.jpg"]["status"], "no_timeline_match")
        self.assertEqual(r["no_sidecar.jpg"]["status"], "skipped_no_reliable_time")
        self.assertEqual(r["clip.mp4"]["status"], "skipped_unsupported_type")

        dest = os.path.join(self.out, r["in_visit.jpg"]["output"])
        tags = json.loads(subprocess.run(
            ["exiftool", "-json", "-n", "-GPSLatitude", "-GPSLongitude", "-GPSLatitudeRef",
             "-GPSLongitudeRef", "-DateTimeOriginal", "-OffsetTimeOriginal", dest],
            capture_output=True, check=True).stdout)[0]
        self.assertAlmostEqual(tags["GPSLatitude"], 51.5, places=5)
        self.assertAlmostEqual(tags["GPSLongitude"], -0.12, places=5)
        self.assertEqual(tags["DateTimeOriginal"], "2024:06:01 10:30:00")
        self.assertEqual(tags["OffsetTimeOriginal"], "+01:00")

        # Original untouched.
        with open(os.path.join(self.takeout, "in_visit.jpg"), "rb") as f:
            self.assertEqual(f.read(), TINY_JPEG)
        # Only tagged files are copied.
        self.assertFalse(os.path.exists(os.path.join(self.out, "Google Photos", "Photos from 2024", "has_geo.jpg")))

    def test_shift_check_blocks_writing(self):
        # Mislabelled Timeline plus camera-geotagged photos along the true route.
        with open(self.timeline, "w") as f:
            json.dump(moving_timeline(3), f)
        for k, (taken, lat, lng) in enumerate(known_photos()):
            name = "gps_%02d.jpg" % k
            path = os.path.join(self.takeout, name)
            with open(path, "wb") as f:
                f.write(TINY_JPEG)
            subprocess.run(["exiftool", "-q", "-overwrite_original", "-GPSLatitude=%f" % lat,
                            "-GPSLatitudeRef=N", "-GPSLongitude=%f" % abs(lng), "-GPSLongitudeRef=W", path],
                           check=True)
            with open(path + ".json", "w") as f:
                json.dump(sidecar(name, taken.isoformat()), f)
        # A photo without GPS that would otherwise be tagged.
        with open(os.path.join(self.takeout, "untagged.jpg"), "wb") as f:
            f.write(TINY_JPEG)
        with open(os.path.join(self.takeout, "untagged.jpg.json"), "w") as f:
            json.dump(sidecar("untagged.jpg", "2024-06-01T10:00:00+00:00"), f)

        takeout = os.path.join(self.tmp, "Takeout")
        g.run(["--timeline", self.timeline, "--takeout", takeout, "--out", self.out])
        self.assertEqual(self.report()["untagged.jpg"]["status"], "would_tag")
        self.assertEqual(sorted(os.listdir(self.out)), ["geotag_report.csv", "time_shift_check.csv"])
        with open(os.path.join(self.out, "time_shift_check.csv"), newline="") as f:
            detail = list(csv.DictReader(f))
        self.assertEqual(len(detail), len(known_photos()))
        self.assertTrue(all(float(d["distance_km"]) > 1 for d in detail if d["distance_km"]))

        g.run(["--timeline", self.timeline, "--takeout", takeout, "--out", self.out, "--time-shift", "3"])
        r = self.report()["untagged.jpg"]
        self.assertEqual(r["status"], "tagged")
        self.assertAlmostEqual(float(r["lng"]), -2.0, places=4)

    def test_dry_run_copies_nothing(self):
        g.run(["--timeline", self.timeline, "--takeout", os.path.join(self.tmp, "Takeout"),
               "--out", self.out, "--dry-run"])
        self.assertEqual(self.report()["in_visit.jpg"]["status"], "would_tag")
        self.assertEqual(os.listdir(self.out), ["geotag_report.csv"])


if __name__ == "__main__":
    unittest.main()
