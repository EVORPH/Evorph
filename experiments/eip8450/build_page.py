"""Inject the generated timeline into the chapter 2 page.

Run after ``timeline.py`` so the page always carries measured data:

    python3 timeline.py > timeline.json && python3 build_page.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE.parent.parent / "frontend" / "dist" / "chapter2" / "index.html"


def main() -> None:
    data = json.loads((HERE / "timeline.json").read_text())
    html = PAGE.read_text()
    start = html.find('<script type="application/json" id="data">')
    end = html.find("</script>", start)
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    PAGE.write_text(html[:start] + f'<script type="application/json" id="data">{payload}' + html[end:])
    print(f"page updated: {PAGE.relative_to(HERE.parent.parent)} rows={len(data['rows'])}")


if __name__ == "__main__":
    main()
