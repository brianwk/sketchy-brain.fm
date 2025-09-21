import argparse
import json
import re
import socket
import time
from typing import Optional, List
from urllib.request import urlopen
import subprocess
from pathlib import Path

APP_PATH = '/Applications/Brain.fm.app/Contents/MacOS/Brain.fm'
TIMER_PATTERN = re.compile(r"\b(?:\d+:)?[0-5]?\d:[0-5]\d\b")


def http_get_json(url: str, timeout: float = 2.0) -> dict:
    with urlopen(url, timeout=timeout) as resp:
        data = resp.read()
        return json.loads(data.decode("utf-8"))


def get_browser_ws_url(port: int) -> str:
    ver = http_get_json(f"http://127.0.0.1:{port}/json/version")
    ws = ver.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError("Browser webSocketDebuggerUrl not found; ensure --remote-debugging-port is set")
    return ws


def is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except Exception:
            return False


def choose_target(targets: List[dict], prefer: Optional[str]) -> Optional[dict]:
    def score(t: dict) -> int:
        sc = 0
        if t.get("type") == "page":
            sc += 10
        ttl = (t.get("title") or "").lower()
        url = (t.get("url") or "")
        if prefer and prefer.lower() in (ttl + " " + url).lower():
            sc += 5
        if "brain" in ttl:
            sc += 3
        if url.startswith("chrome-extension://"):
            sc -= 5
        if url.startswith("devtools://"):
            sc -= 10
        return sc

    if not targets:
        return None
    return sorted(targets, key=score, reverse=True)[0]


class BrowserCDP:
    def __init__(self, ws_url: str) -> None:
        from websocket import create_connection  # type: ignore

        self.ws = create_connection(ws_url, timeout=5)
        self.msg_id = 0

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> int:
        self.msg_id += 1
        payload = {"id": self.msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        self.ws.send(json.dumps(payload))
        return self.msg_id

    def recv_until(self, id_wanted: int) -> dict:
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == id_wanted:
                return data


def find_timer(text: str) -> Optional[str]:
    if not text:
        return None
    matches = TIMER_PATTERN.findall(text)
    if not matches:
        return None
    matches.sort(key=lambda s: (-s.count(":"),))
    return matches[0]


def update_sketchybar(item: str, timer: str) -> None:
    # Use subprocess to avoid shell quoting issues
    try:
        if timer:
            subprocess.run(["sketchybar", "--set", item, "label.drawing=on", f"label={timer}"], check=False, capture_output=True)
        else:
            subprocess.run(["sketchybar", "--set", item, "label.drawing=off"], check=False, capture_output=True)
    except FileNotFoundError:
        # sketchybar not installed; ignore but print for visibility
        print(f"[warn] sketchybar not found; timer={timer}")


def sketchybar_item_exists(item: str) -> bool:
    try:
        res = subprocess.run(["sketchybar", "--query", item], capture_output=True, text=True, check=False)
        return res.returncode == 0 and (res.stdout.strip() != "")
    except FileNotFoundError:
        return False


def ensure_sketchybar_item(item: str, position: str = "right") -> None:
    if sketchybar_item_exists(item):
        return
    # Properly split args: --add item <name> <position>
    subprocess.run(["sketchybar", "--add", "item", item, position], check=False, capture_output=True)


def find_brainfm_app_path() -> Optional[Path]:
    # Use Spotlight to find the app by bundle id
    try:
        res = subprocess.run(
            ["mdfind", "kMDItemCFBundleIdentifier=com.electron.brain.fm"],
            capture_output=True,
            text=True,
            check=False,
        )
        paths = [Path(p.strip()) for p in res.stdout.splitlines() if p.strip() and p.strip().endswith(".app")]
        if not paths:
            return None
        # Prefer /Applications installs
        paths.sort(key=lambda p: (not str(p).startswith("/Applications/"), len(str(p))))
        return paths[0]
    except Exception:
        return None


def ensure_sketchybar_icon(item: str) -> None:
    """Try to set the app icon as sketchybar icon.image once. No-op on failure."""
    try:
        app_path = find_brainfm_app_path()
        if not app_path:
            return
        res_dir = app_path / "Contents" / "Resources"
        if not res_dir.is_dir():
            return
        icns = None
        candidates = list(res_dir.glob("*.icns"))
        icns = candidates.pop() if candidates else None
        if not icns:
            return

        cache_dir = Path.home() / ".cache" / "brain_test"
        cache_dir.mkdir(parents=True, exist_ok=True)
        png_out = cache_dir / "brainfm_icon.png"

        # Convert with sips and downscale to ~36px for menu bar
        subprocess.run(["sips", "-s", "format", "png", str(icns), "--out", str(png_out)], check=False, capture_output=True)
        if not png_out.exists():
            return
        # Optional downscale to 36px max dimension
        subprocess.run(["sips", "-Z", "36", str(png_out)], check=False, capture_output=True)
        print(png_out)
        print("adding sketchybar item", item)
        print("Added:", subprocess.run(["sketchybar", "--add", "item", item, "right"], check=False, capture_output=True))
        subprocess.run(["sketchybar", "--set", item, 'click_script=open /Applications/Brain.fm.app/', 'icon.drawing=on', 'icon.padding_right=0', 'icon.padding_left=0', 'padding_left=1', 'padding_right=1', 'icon.color=transparent', 'icon=⬛', 'background.corner_radius=5', 'background.color=0x66f0f0f0', 'background.height=20', 'background.drawing=on', 'background.image.scale=0.6', f'background.image={png_out}'], check=False, capture_output=True)
    except Exception:
        # Ignore all errors—icon is optional
        pass

def launch_brainfm_app() -> None:
    app_path = Path(APP_PATH)
    if not app_path.exists():
        app_path = find_brainfm_app_path()
    if not app_path or not app_path.exists():
        print("Brain.fm app not found; please install it from https://brain.fm/")
        return
    try:
        subprocess.Popen([str(app_path), "--remote-debugging-port=9222", "--remote-allow-origins=*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Launched Brain.fm app.")
        time.sleep(5)  # Give it some time to start
    except Exception as e:
        print(f"Failed to launch Brain.fm app: {e}")
        raise e
        
def main() -> int:
    ap = argparse.ArgumentParser(description="Brain.fm timer via CDP (poll + sketchybar)")
    ap.add_argument("--port", type=int, default=9222, help="Remote debugging port (default 9222)")
    ap.add_argument("--ws", type=str, help="Override browser WebSocket URL (from /json/version)")
    ap.add_argument("--selector", type=str, help="CSS selector for the timer element (querySelector)")
    ap.add_argument("--target-contains", type=str, help="Prefer targets whose title/URL contains this")
    ap.add_argument("--interval", type=float, default=0.1, help="Polling interval seconds (default 0.1)")
    ap.add_argument("--item", type=str, default="brain_timer", help="sketchybar item name (default brain_timer)")
    ap.add_argument("--position", type=str, default="right", help="sketchybar position: left|center|right (default right)")
    args = ap.parse_args()

    # CDP dependency check
    try:
        import websocket  # noqa: F401
    except Exception:
        print("Missing dependency 'websocket-client'. Install: pip install websocket-client")
        return 2

    if not args.ws and not is_port_open("127.0.0.1", args.port):
        launch_brainfm_app()
        #print(f"Port {args.port} is not open. Launch the app with --remote-debugging-port={args.port} and --remote-allow-origins=http://127.0.0.1:{args.port}")
        #return 2

    ws_url = args.ws or get_browser_ws_url(args.port)
    client = BrowserCDP(ws_url)
    session_id: Optional[str] = None
    last_timer: Optional[str] = None

    try:
        # Attach to the best page target
        rid = client.send("Target.getTargets")
        resp = client.recv_until(rid)
        targets = resp.get("result", {}).get("targetInfos", [])
        target = choose_target(targets, args.target_contains)
        if not target or target.get("type") != "page":
            print("No suitable page target found.")
            return 3
        rid = client.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        attach_resp = client.recv_until(rid)
        session_id = attach_resp.get("result", {}).get("sessionId")
        if not session_id:
            print("Failed to attach to page target.")
            return 4

        client.send("Runtime.enable", session_id=session_id)

        # Try to set the icon once (best effort)
        ensure_sketchybar_item(args.item, args.position)
        ensure_sketchybar_icon(args.item)

        # Build expression: search inline style first, then optional selector, then body text
        selector_expr = json.dumps(args.selector) if args.selector else "null"
        expr = (
            "(function(){\n"
            "  var r=/(?:\\d+:)?[0-5]?\\d:[0-5]\\d/;\n"
            "  function fromInlineStyle(){\n"
            "    try{\n"
            "      var nodes=document.querySelectorAll('[style]');\n"
            "      for(var i=0;i<nodes.length;i++){\n"
            "        var el=nodes[i];\n"
            "        var st=(el.getAttribute('style')||'').toLowerCase();\n"
            "        if(st.includes('font-variant-numeric') && st.includes('tabular-nums')){\n"
            "          var txt=(el.textContent||'').trim();\n"
            "          var m=txt.match(r);\n"
            "          if(m) return m[0];\n"
            "          if(txt) return txt;\n"
            "        }\n"
            "      }\n"
            "    }catch(e){}\n"
            "    return '';\n"
            "  }\n"
            f"  var s={selector_expr};\n"
            "  var t=fromInlineStyle(); if(t) return t;\n"
            "  if(s){ var el=document.querySelector(s); if(el){ var txt=(el.textContent||'').trim(); var m=txt.match(r); if(m) return m[0]; if(txt) return txt; } }\n"
            "  if(document && document.body){ var b=(document.body.innerText||'').trim(); var m=b.match(r); if(m) return m[0]; return b; }\n"
            "  return '';\n"
            "})()"
        )

        # Poll loop
        while True:
            rid = client.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id=session_id)
            eval_resp = client.recv_until(rid)
            text_val = eval_resp.get("result", {}).get("result", {}).get("value", "")
            timer = find_timer(text_val)
            if timer and timer != last_timer:
                update_sketchybar(args.item, timer)
                last_timer = timer
            time.sleep(args.interval)
    except KeyboardInterrupt:
        update_sketchybar(args.item, "")  # Clear on exit
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())