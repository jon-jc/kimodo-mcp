"""Explicit, bounded 2D interpolation for short internal detection gaps."""

from copy import deepcopy

import numpy as np


def bridge_detections(records: list[dict], maximum: int) -> tuple[list[dict], list[int]]:
    """Keep observations unchanged; attach separate low-confidence fitting inputs.

    This is interpolation, not recovered observation or 3D occlusion reasoning.
    Never extrapolate endpoints, bridge ambiguous people, or recursively use an
    earlier estimate as an endpoint of another estimate.
    """
    if type(maximum) is not int or not 0 <= maximum <= 15:
        raise ValueError("Gap bridging must be zero to 15 frames")
    result, estimated = deepcopy(records), []
    if not maximum:
        return result, estimated
    index = 0
    while index < len(records):
        if records[index]["status"] == "detected":
            index += 1
            continue
        start = index
        while index < len(records) and records[index]["status"] != "detected":
            index += 1
        end = index
        if start == 0 or end == len(records) or end - start > maximum:
            continue
        gap = records[start:end]
        if any(item["status"] not in {"missing", "insufficient"}
               or item.get("person_id") not in {None, 0} for item in gap):
            continue
        left = np.asarray(records[start - 1]["keypoints"], dtype=float)
        right = np.asarray(records[end]["keypoints"], dtype=float)
        if left.shape != (25, 3) or right.shape != (25, 3) or not np.isfinite([left, right]).all():
            continue
        confidence = np.minimum(np.minimum(left[:, 2], right[:, 2]), 0.25)
        usable = confidence > 0.05
        if usable.sum() < 5:
            continue
        # Refuse gross discontinuities; this is not a general shot-cut detector.
        span = max(np.linalg.norm(np.ptp(left[usable, :2], axis=0)), 5)
        if np.max(np.linalg.norm(right[usable, :2] - left[usable, :2], axis=1)) > span:
            continue
        candidates = []
        for frame in range(start, end):
            fraction = (frame - start + 1) / (end - start + 1)
            points = left * (1 - fraction) + right * fraction
            points[:, 2] = confidence
            candidates.append((frame, fraction, points))
        if any((np.ptp(points[usable, :2], axis=0) < 5).any() for _, _, points in candidates):
            continue
        for frame, fraction, points in candidates:
            result[frame].update(fitted_keypoints=points.tolist(), estimated=True,
                                 estimate={"method": "linear_2d_interpolation", "before_frame": start - 1,
                                           "after_frame": end, "fraction": fraction,
                                           "confidence_cap": 0.25})
            estimated.append(frame)
    return result, estimated
