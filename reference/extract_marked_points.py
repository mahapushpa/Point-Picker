#!/usr/bin/env python3
"""
extract_marked_points.py

Companion to point_picker.html, for the alternative workflow: mark points on an
image using any editor (Paint, Preview, etc.) with colored circles/dots, then
extract their exact pixel coordinates automatically.

This is the method used successfully across a long back-and-forth of manually
correcting points on a scanned map: draw small colored circles by hand, run
this script, get back precise (x, y) coordinates clustered and de-duplicated.

Usage:
    python3 extract_marked_points.py marked_image.png --color red
    python3 extract_marked_points.py marked_image.png --color blue --offset 1900 1350

Requires: opencv-python, numpy
    pip install opencv-python numpy
"""

import argparse
import json
import sys
import numpy as np
import cv2

COLOR_PRESETS = {
    # (name): function(b,g,r arrays) -> boolean mask
    "red":   lambda b, g, r: (r.astype(int) - g.astype(int) > 50) & (r.astype(int) - b.astype(int) > 50) & (r > 120),
    "blue":  lambda b, g, r: (b.astype(int) - r.astype(int) > 15) & (b > 100) & (r < 150),
    "green": lambda b, g, r: (g.astype(int) - r.astype(int) > 40) & (g.astype(int) - b.astype(int) > 40) & (g > 100),
}


def find_markers(image_path, color, min_area=60, cluster_tol=20):
    img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Could not read image: {image_path}")
    b, g, r = cv2.split(img)
    mask = COLOR_PRESETS[color](b, g, r).astype(np.uint8) * 255

    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    raw_points = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        (cx, cy), _ = cv2.minEnclosingCircle(c)
        raw_points.append((cx, cy))

    # cluster nearby detections (a single hand-drawn circle often yields
    # multiple contours -- inner/outer edge, or a filled-in blob)
    clusters = []
    used = [False] * len(raw_points)
    for i in range(len(raw_points)):
        if used[i]:
            continue
        group = [raw_points[i]]
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j in range(len(raw_points)):
                if used[j]:
                    continue
                if any(np.hypot(raw_points[j][0] - gx, raw_points[j][1] - gy) < cluster_tol
                       for gx, gy in group):
                    group.append(raw_points[j])
                    used[j] = True
                    changed = True
        cx = float(np.mean([p[0] for p in group]))
        cy = float(np.mean([p[1] for p in group]))
        clusters.append((cx, cy))

    return clusters


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="Path to the annotated image")
    ap.add_argument("--color", choices=list(COLOR_PRESETS), default="red",
                     help="Marker color to detect (default: red)")
    ap.add_argument("--offset", nargs=2, type=float, default=(0, 0), metavar=("X", "Y"),
                     help="Offset to add to coordinates, e.g. if this image is a crop "
                          "of a larger original (default: 0 0)")
    ap.add_argument("--min-area", type=float, default=60,
                     help="Minimum contour area to count as a marker (filters noise)")
    ap.add_argument("--sort", choices=["reading", "none"], default="reading",
                     help="Sort order for output: 'reading' = top-to-bottom, left-to-right")
    ap.add_argument("-o", "--output", help="Write JSON to this file instead of stdout")
    args = ap.parse_args()

    clusters = find_markers(args.image, args.color, min_area=args.min_area)
    ox, oy = args.offset
    points = [{"x": round(cx + ox, 1), "y": round(cy + oy, 1)} for cx, cy in clusters]

    if args.sort == "reading":
        points.sort(key=lambda p: (round(p["y"] / 60), p["x"]))

    for i, p in enumerate(points, start=1):
        p["label"] = str(i)

    out = json.dumps(points, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
        print(f"Wrote {len(points)} points to {args.output}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
