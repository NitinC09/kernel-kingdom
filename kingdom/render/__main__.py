"""Wallpaper renderer.

    python -m kingdom.render                    # wallpaper (layer-shell, BACKGROUND layer)
    python -m kingdom.render --windowed         # normal window, for development
    python -m kingdom.render --snapshot out.png # one frame to PNG, no GTK
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from .. import config, i18n, paths

LAYER_SHELL_LIB = "libgtk4-layer-shell.so"


def _ensure_layer_shell_preload():
    """gtk4-layer-shell has to be loaded before libwayland-client.
    From Python the only reliable way is LD_PRELOAD, so we re-exec ourselves."""
    if not sys.platform.startswith("linux") or LAYER_SHELL_LIB in os.environ.get("LD_PRELOAD", ""):
        return                               # layer-shell is Wayland-only
    for d in ("/usr/lib64", "/usr/lib", "/usr/lib/x86_64-linux-gnu"):
        for lib in sorted(Path(d).glob(LAYER_SHELL_LIB + "*")):  # Fedora only ships .so.0
            env = dict(os.environ, LD_PRELOAD=f"{lib} {os.environ.get('LD_PRELOAD', '')}".strip())
            os.execve(sys.executable, [sys.executable, "-m", "kingdom.render", *sys.argv[1:]], env)
    print("warning: gtk4-layer-shell not found, using a normal window", file=sys.stderr)


def snapshot(args, cfg):
    """Renders one frame to PNG as it would look on a --size screen."""
    import cairo

    from .iso import Painter, detail_for, fit_scale, zoom_scales

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    painter = Painter(hemisphere=cfg["render"]["hemisphere"])
    painter.hour_override = args.hour
    painter.season_override = args.season
    painter.window_mode = args.window
    sw, sh = (int(v) for v in args.size.split("x"))
    now = time.monotonic()
    painter.set_state(state, now)
    # force this tick's events to show in the picture
    painter.bolts = [dict(ev, start=now - 0.03, seed=k / 7) for k, ev in enumerate(state.get("events", []))]
    base = fit_scale(sw, sh, painter.content_size(), cfg["render"]["pixel_scale"])
    scales = zoom_scales(base)
    scale = scales[min(args.zoom, len(scales) - 1)]
    if args.focus:   # center the camera on a faction's capital
        f = next(f for f in state["factions"] if f["name"] == args.focus)
        x, y = f["capital"]
        wx, wy = painter.tile_xy(x, y)
        ww, wh = painter.world_size()
        painter.pan = [wx - ww / 2, wy - wh / 2]
    fw, fh = int(-(-sw // scale)), int(-(-sh // scale))
    painter.set_detail(detail_for(scale))
    t0 = time.perf_counter()
    painter.frame(fw, fh, now)                 # first frame: includes the static base
    t1 = time.perf_counter()
    for k in range(30):
        painter.frame(fw, fh, now + 0.001 * k)   # animated frames
    t2 = time.perf_counter()
    if args.click:
        px, py = (float(v) for v in args.click.split(","))
        painter.inspect(px / scale, py / scale, now, ui_pos=(px / base, py / base))
        painter.hover = painter.pick(px / scale, py / scale)
    surf = painter.frame(fw, fh, now + 0.05)
    ui, _ = painter.ui(int(-(-sw // base)), int(-(-sh // base)), now + 0.05)
    big = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
    ctx = cairo.Context(big)
    for layer, k in ((surf, scale / painter.u), (None if args.no_ui else ui, base)):
        if layer is None:
            continue
        ctx.save()
        ctx.scale(k, k)
        ctx.set_source_surface(layer, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_NEAREST)
        ctx.paint()
        ctx.restore()
    big.write_to_png(args.snapshot)
    print(f"{args.snapshot}: scale {scale:g} ({fw}x{fh} logical), base {1000 * (t1 - t0):.1f} ms, "
          f"animated frame {1000 * (t2 - t1) / 30:.2f} ms")


def main():
    ap = argparse.ArgumentParser(prog="kingdom.render")
    ap.add_argument("--windowed", action="store_true", help="normal window instead of wallpaper")
    ap.add_argument("--mini", action="store_true", help="small window with an automatic camera")
    ap.add_argument("--monitor", help="wallpaper monitor: index (0, 1...), connector (DP-1), or all (Windows: one process)")
    ap.add_argument("--state", default=str(paths.state_file()))
    ap.add_argument("--config", type=Path)
    ap.add_argument("--fps", type=int, help="FPS cap (max. 30)")
    ap.add_argument("--no-occlusion", action="store_true", help="don't pause behind maximized windows")
    ap.add_argument("--snapshot", metavar="PNG")
    ap.add_argument("--size", default="2560x1440", help="screen size for --snapshot")
    ap.add_argument("--click", metavar="X,Y", help="--snapshot: simulate a click (screen px)")
    ap.add_argument("--season", choices=["winter", "spring", "summer", "autumn"])
    ap.add_argument("--zoom", type=int, default=0, help="--snapshot: extra zoom levels")
    ap.add_argument("--focus", help="--snapshot: center on this faction's capital (e.g. chrome)")
    ap.add_argument("--no-ui", action="store_true", help="--snapshot: no HUD or chronicle")
    ap.add_argument("--window", action="store_true", help="--snapshot: window-mode interface (lists, powers)")
    ap.add_argument("--hour", type=float, help="force the hour of the day (0-24)")
    ap.add_argument("--start-delay", type=float, default=0, help=argparse.SUPPRESS)   # relanzarse tras reiniciar Explorer
    args = ap.parse_args()
    cfg = config.load(args.config)
    i18n.set_lang(cfg.get("language", "auto"))

    if args.snapshot:
        return snapshot(args, cfg)
    if args.start_delay:
        time.sleep(args.start_delay)
    if not args.windowed:
        _ensure_layer_shell_preload()
    if sys.platform == "win32" or os.environ.get("KK_BACKEND") == "sdl":   # Windows: pygame-ce (SDL2), no GTK
        from .sdl_app import run
    else:
        from .app import run
    run(args, cfg)


if __name__ == "__main__":
    main()
