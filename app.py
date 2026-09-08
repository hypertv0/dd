import json
import os
import re
import requests
from urllib.parse import urljoin, urlencode, quote
from flask import Flask, request, Response

app = Flask(__name__)

DATA = json.load(open(os.path.join(os.path.dirname(__file__), "channels.json"), encoding="utf-8"))
CHANNELS = DATA["channels"]

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


# Distinct header sets the playlist needs -> each proxied channel gets an index.
HEADERS = []
HINDEX = {}


def header_index(h):
    key = json.dumps(h or {}, sort_keys=True)
    if key not in HINDEX:
        HINDEX[key] = len(HEADERS)
        HEADERS.append(h or {})
    return HINDEX[key]


for c in CHANNELS:
    c["hindex"] = header_index(c.get("headers") or {}) if c.get("headers") else None
    c["needs_proxy"] = bool(c.get("headers"))


def base():
    return request.url_root.rstrip("/")


def proxy_url(target, hindex):
    return base() + "/p?" + urlencode({"u": target, "h": hindex})


def rewrite_manifest(text, source_url, hindex):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if "URI=" in s:
                s = re.sub(
                    r'URI=["\']([^"\']+)["\']',
                    lambda m: f'URI="{proxy_url(urljoin(source_url, m.group(1)), hindex)}"',
                    s,
                )
            out.append(s)
        else:
            out.append(proxy_url(urljoin(source_url, s), hindex))
    return "\n".join(out)


def fetch(target_url, hindex, stream=False):
    headers = {"User-Agent": DEFAULT_UA}
    headers.update(HEADERS[hindex] or {})
    return requests.get(target_url, headers=headers, stream=stream, timeout=15)


@app.route("/playlist.m3u8")
@app.route("/playlist.m3u")
def playlist():
    lines = ["#EXTM3U"]
    for c in CHANNELS:
        lines.append(
            f'#EXTINF:-1 tvg-id="{c["id"]}" tvg-logo="{c.get("logo") or ""}" '
            f'group-title="{c.get("group") or ""}",{c["name"]}'
        )
        if c["needs_proxy"]:
            lines.append(proxy_url(c["url"], c["hindex"]))
        else:
            lines.append(c["url"])
    return Response("\n".join(lines), content_type="application/vnd.apple.mpegurl")


@app.route("/p")
def proxy():
    target = request.args.get("u")
    hindex = int(request.args.get("h", 0))
    if not target:
        return "no url", 400
    try:
        r = fetch(target, hindex, stream=True)
    except requests.RequestException as e:
        return f"upstream error: {e}", 502
    if r.status_code != 200:
        return f"upstream {r.status_code}", r.status_code

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "mpegurl" in ctype or ".m3u8" in target.lower():
        return Response(
            rewrite_manifest(r.text, target, hindex),
            content_type="application/vnd.apple.mpegurl",
        )

    def gen():
        for chunk in r.iter_content(chunk_size=64 * 1024):
            yield chunk

    return Response(gen(), content_type=r.headers.get("Content-Type", "video/mp2t"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5757)))
