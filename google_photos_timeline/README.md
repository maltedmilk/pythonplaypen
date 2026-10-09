# Geotag Google Photos from Google Maps Timeline

`geotag_from_timeline.py` finds photos in a Google Photos Takeout export that have no location,
looks up where you were at the time they were taken in your Timeline export, and writes the
GPS coordinates into **copies** of those photos. You then upload the copies to Google Photos.

Google's API can't change locations on existing Google Photos items, so this works from exported copies.

## What it does

For each photo:

1. **Skips** photos that already have a location, either in EXIF or in the Takeout `.json` sidecar.
2. Gets the capture time from the sidecar (`photoTakenTime`, which is in UTC). If there's no sidecar,
   it uses EXIF `DateTimeOriginal` + `OffsetTimeOriginal`. Photos with neither are skipped, because
   their timezone is unknown.
3. Picks a location from the Timeline:
   - `visit`: the photo was taken during a place visit, so that place's coordinates are used.
   - `interpolated`: the photo falls between two path points that are each within `--max-gap` minutes.
   - `nearest:*`: only one point is within `--max-gap` minutes.
4. Copies the photo to `--out` (same folder structure) and writes GPS EXIF tags to the copy with ExifTool.
   If the photo has no `DateTimeOriginal`, one is added from the sidecar time, so the upload
   doesn't get dated by its upload date.

Supported types: JPEG, HEIC/HEIF, TIFF, DNG. Videos and other formats are listed as
`skipped_unsupported_type` in the report.

## Steps (Windows)

1. **Export Timeline** on your Android phone: Settings > Location > Location services > Timeline >
   Export Timeline data. Copy `Timeline.json` to your PC.
2. **Export Google Photos** from <https://takeout.google.com> (select only Google Photos). Extract all
   the zip files into one folder, e.g. `C:\Takeout`.
3. **Install ExifTool** from <https://exiftool.org>. Use the Windows executable and follow the install
   notes on that page so `exiftool` runs from a command prompt. Or pass its full path with `--exiftool`.
4. **Dry run first.** This writes only the report:

   ```
   python geotag_from_timeline.py --timeline Timeline.json --takeout C:\Takeout --out C:\Geotagged --dry-run
   ```

   Open `C:\Geotagged\geotag_report.csv` and spot-check some `would_tag` rows. Paste the `lat,lng`
   into Google Maps to confirm the location. Adjust `--max-gap` if needed.
5. **Run for real.** Use the same command without `--dry-run`.
6. **Upload** the contents of `C:\Geotagged` (not the CSV) at <https://photos.google.com> (Upload button
   or drag and drop).

## Things to know before uploading

- **Duplicates.** The tagged copies are new files, so Google Photos treats them as new items. The
  originals stay in your library until you delete them. The report's `file` column lists which
  originals have a tagged copy.
- **Albums, descriptions, favourites, faces and edits made in Google Photos** belong to the original
  item. They don't carry over to the uploaded copy. If you delete the original, it is also removed
  from its albums.
- **Storage.** Uploads count toward your Google storage quota.
- **Edited copies.** Takeout can include both `IMG_1.jpg` and `IMG_1-edited.jpg`. Both get tagged if
  both lack a location. Delete whichever you don't want before uploading.
- Timeline locations come from your phone, so they can be wrong, e.g. a wrong place for a visit, or a
  photo taken on a camera whose clock was off. Check the report.

## Report statuses

| status | meaning |
|---|---|
| `tagged` / `would_tag` | location written (or would be, in a dry run) |
| `skipped_has_location` | photo already has a location |
| `skipped_no_reliable_time` | no sidecar, and no EXIF time with a timezone |
| `no_timeline_match` | no Timeline data within `--max-gap` minutes |
| `skipped_unsupported_type` | not a supported image type (e.g. video) |
| `write_failed` | ExifTool reported an error for this file |

## Tests

```
python -m unittest test_geotag_from_timeline
```

The end-to-end tests are skipped if `exiftool` isn't installed.
