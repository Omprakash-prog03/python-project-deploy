"""End-to-end smoke test against a running PlateVision instance.

    python scripts/smoke.py [--base http://localhost:8000]

Exercises the path an operator and an integrator actually take: login → read a
frame → store it → review it → watchlist enforcement → analytics → export.
Exits non-zero on the first failure, so it is safe to use in CI or a deploy
hook.
"""
from __future__ import annotations

import argparse
import io
import sys

import httpx
from PIL import Image


def _frame(text: str = "MH12AB3456") -> bytes:
    """Render a plate on a plain background without needing app imports."""
    from PIL import ImageDraw, ImageFont

    plate = Image.new("RGB", (430, 136), (252, 250, 242))
    draw = ImageDraw.Draw(plate)
    draw.rounded_rectangle([3, 3, 426, 132], radius=8, outline=(18, 18, 22), width=3)
    draw.rectangle([3, 3, 42, 132], fill=(20, 52, 140))
    try:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        size = 84
        while size > 20 and draw.textlength(text, font=ImageFont.truetype(path, size)) > 350:
            size -= 4
        font = ImageFont.truetype(path, size)
    except Exception:  # pragma: no cover - font optional
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((60, (136 - (box[3] - box[1])) / 2 - box[1]), text, fill=(14, 14, 18), font=font)
    canvas = Image.new("RGB", (640, 320), (30, 32, 40))
    canvas.paste(plate, (105, 92))
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=93)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--email", default="elara@platevision.io")
    parser.add_argument("--password", default="nightwatch")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    checks: list[tuple[str, bool, str]] = []

    with httpx.Client(base_url=base, timeout=60.0) as client:
        r = client.get("/healthz")
        checks.append(("liveness", r.status_code == 200, r.text[:40]))

        r = client.get("/api/v1/health")
        body = r.json() if r.status_code == 200 else {}
        checks.append(
            ("health", r.status_code == 200 and body.get("status") == "ok",
             f"{body.get('database')}/{body.get('storage')}/{body.get('engine')}")
        )

        r = client.post("/api/v1/auth/login", json={"email": args.email, "password": args.password})
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
        checks.append(("login", True, r.json()["user"]["full_name"]))

        r = client.post(
            "/api/v1/recognize",
            files={"file": ("frame.jpg", _frame(), "image/jpeg")},
            data={"country": "IN"},
        )
        best = (r.json().get("best") or {}) if r.status_code == 200 else {}
        checks.append(
            ("recognize", r.status_code == 200 and bool(best),
             f"{best.get('plate')} @ {round(best.get('confidence', 0) * 100)}% "
             f"in {r.json().get('processing_ms', 0)} ms")
        )

        r = client.get("/api/v1/cameras")
        cameras = r.json() if r.status_code == 200 else []
        checks.append(("cameras", bool(cameras), f"{len(cameras)} registered"))

        camera_id = cameras[0]["id"] if cameras else None
        data = {"country": "IN"}
        if camera_id:
            data["camera_id"] = camera_id
        r = client.post(
            "/api/v1/captures",
            files={"file": ("frame.jpg", _frame("MH12AB3456"), "image/jpeg")},
            data=data,
        )
        capture = r.json() if r.status_code == 201 else {}
        checks.append(
            ("ingest", r.status_code == 201,
             f"{capture.get('plate')} · {capture.get('status')} · risk {capture.get('risk_level')}")
        )
        checks.append(("watchlist enforcement", bool(capture.get("watchlist_hit")), "capture flagged"))

        if capture.get("id"):
            r = client.patch(
                f"/api/v1/captures/{capture['id']}",
                json={"status": "verified", "notes": "smoke test"},
            )
            checks.append(("review", r.status_code == 200 and r.json()["status"] == "verified", "verified"))
            r = client.get(f"/api/v1/captures/{capture['id']}/image")
            checks.append(("evidence image", r.status_code == 200, f"{len(r.content)} bytes"))

        r = client.get("/api/v1/analytics/overview?range=7d")
        kpis = r.json().get("kpis", {}) if r.status_code == 200 else {}
        checks.append(("analytics", r.status_code == 200, f"{kpis.get('scans')} scans, "
                                                           f"{kpis.get('accuracy')}% accuracy"))

        r = client.get("/api/v1/watchlist")
        checks.append(("watchlist", r.status_code == 200, f"{len(r.json())} entries"))

        r = client.get("/api/v1/captures/export.csv")
        checks.append(("csv export", r.status_code == 200 and b"capture_id" in r.content,
                       f"{len(r.content)} bytes"))

        r = client.get("/")
        checks.append(("console", r.status_code == 200 and "PlateVision" in r.text, "index served"))

        r = client.get("/api/v1/metrics")
        checks.append(("metrics", r.status_code == 200, "prometheus exposition"))

    width = max(len(name) for name, _, _ in checks)
    failures = 0
    print()
    for name, ok, detail in checks:
        if not ok:
            failures += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print(f"All {len(checks)} checks passed against {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
