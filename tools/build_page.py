"""页面构建：将 frontend/src 下的 HTML/CSS/JS 打包为单文件 frontend/dist/index.html。"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
DIST = ROOT / "frontend" / "dist"


def main() -> int:
    html = (SRC / "index.html").read_text(encoding="utf-8")
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    js = (SRC / "app.js").read_text(encoding="utf-8")

    html = html.replace(
        '<link rel="stylesheet" href="styles.css">',
        f"<style>\n{css}\n</style>",
    ).replace(
        '<script src="app.js"></script>',
        f"<script>\n{js}\n</script>",
    )

    if "<style>" not in html or "<script>" not in html:
        print("构建失败：样式或脚本内联未完成", file=sys.stderr)
        return 1
    if 'href="styles.css"' in html or 'src="app.js"' in html:
        print("构建失败：仍存在未内联的资源引用", file=sys.stderr)
        return 1

    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:12]
    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "index.html").write_text(f"<!-- build:{digest} -->\n" + html,
                                     encoding="utf-8")
    print(f"页面构建完成 frontend/dist/index.html sha256:{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
