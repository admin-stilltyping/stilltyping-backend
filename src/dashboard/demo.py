"""Synthetic values only. Never reads or writes operational business records."""

import hashlib
import random
from datetime import timedelta

from .catalog import PROFILES


def widget_data(widget_type, use_case, seed, as_of, period):
    # Separate streams mean changing the selected charts never changes another chart's values.
    digest = hashlib.sha256(f"{seed}:{use_case}:{widget_type}:{period}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    profile = PROFILES[use_case]
    scale = period / 30
    total = round(rng.randint(240, 490) * scale)
    target = round(profile["target"] * scale)
    points = []
    for index in range(7 if period == 7 else 10):
        count = 7 if period == 7 else 10
        day = as_of - timedelta(days=round((count - index - 1) * (period - 1) / (count - 1)))
        points.append(
            {
                "label": day.strftime("%d %b"),
                "value": rng.randint(18, 65) * max(1, period // 10),
                "secondary": rng.randint(10, 45) * max(1, period // 10),
            }
        )
    data = {
        "value": total,
        "delta": round(rng.uniform(-6, 22), 1),
        "unit": profile["unit"],
        "target": target,
        "points": points,
        "series_labels": profile["series"],
    }
    if widget_type == "gauge":
        data.update(value=rng.randint(78, 97), unit="%", target=100)
    elif widget_type == "progress":
        data.update(value=round(target * rng.uniform(0.55, 0.88)))
    elif widget_type == "sparkline":
        data["unit"] = (
            "customers"
            if use_case == "support"
            else {"clinic": "patients", "retail": "customers", "services": "clients"}[use_case]
        )
    elif widget_type == "stacked_bar":
        data["series_labels"] = profile["channels"]
    elif widget_type in {"horizontal_bar", "table", "pie", "donut"}:
        labels = (
            profile["segments"]
            if widget_type == "pie"
            else profile["statuses"]
            if widget_type == "donut"
            else profile["categories"]
        )
        data["points"] = sorted(
            [{"label": label, "value": round(rng.randint(28, 145) * scale)} for label in labels],
            key=lambda point: point["value"],
            reverse=True,
        )
        data["value"] = sum(point["value"] for point in data["points"])
    elif widget_type == "funnel":
        value = round(1200 * scale)
        stages = []
        for label in profile["stages"]:
            stages.append({"label": label, "value": value})
            value = round(value * rng.uniform(0.58, 0.83))
        data["points"] = stages
    elif widget_type == "heatmap":
        data["cells"] = [
            {"day": day, "hour": hour, "value": rng.randint(0, 40)}
            for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            for hour in ["08–10", "10–12", "12–14", "14–16", "16–18", "18–20"]
        ]
    elif widget_type == "activity":
        data["items"] = [
            {
                "title": title,
                "detail": f"Sample record DEMO-{1024 + index}",
                "time": f"{9 + index:02}:15",
            }
            for index, title in enumerate(profile["events"])
        ]
    return data
