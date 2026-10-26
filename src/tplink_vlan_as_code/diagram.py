"""Render the config as a draw.io diagram: one VLAN x port matrix per switch, cables between them.

Switches are laid out as a tree following `upstream` links (roots on top). Cells
where the two ends of a switch-to-switch cable disagree are outlined in red.
All free text is HTML-escaped: draw.io renders labels as HTML.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from html import escape

from .config import Config, PortSpec

PALETTE = ["#7E57C2", "#1E88E5", "#43A047", "#FB8C00", "#00897B", "#E53935", "#6D4C41", "#D81B60", "#546E7A", "#C0CA33"]
LBLW, PW, PSTEP, HDR, ROWH, PVH, TOP = 92, 66, 70, 34, 18, 20, 44
LEVEL_GAP, SWITCH_GAP, MARGIN = 170, 60, 20
TXT = "text;html=1;strokeColor=none;fillColor=none;whiteSpace=wrap;"
TRUNK = "strokeColor=#263238;strokeWidth=4;"


def tint(color: str, a: float) -> str:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{int(255 - (255 - x) * a):02X}" for x in (r, g, b))


def mode(port: PortSpec, vid: int) -> str | None:
    return "U" if port.untagged == vid else "T" if vid in port.tagged else None


def render(cfg: Config) -> str:
    return _Diagram(cfg).build()


class _Diagram:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.vids = sorted(cfg.vlans)
        self.color = {v: cfg.vlans[v].color or PALETTE[i % len(PALETTE)] for i, v in enumerate(self.vids)}
        self.colh = HDR + ROWH * len(self.vids) + PVH
        self.swh = TOP + self.colh + 12
        self.n = 0
        self.root = ET.Element("mxfile", host="tplink-vlan-as-code")
        dg = ET.SubElement(self.root, "diagram", name="L2 VLAN topology", id="l2")
        self.model = ET.SubElement(dg, "mxGraphModel", grid="1", gridSize="10", page="1", pageScale="1",
                                   math="0", shadow="0")
        self.cells = ET.SubElement(self.model, "root")
        ET.SubElement(self.cells, "mxCell", id="0")
        ET.SubElement(self.cells, "mxCell", id="1", parent="0")

    # -- primitives
    def _id(self, prefix: str = "c") -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def box(self, label: str, x: float, y: float, w: float, h: float, style: str, id: str | None = None) -> str:
        id = id or self._id()
        c = ET.SubElement(self.cells, "mxCell", id=id, value=label, style=style, vertex="1", parent="1")
        ET.SubElement(c, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h), **{"as": "geometry"})
        return id

    def edge(self, src: str, tgt: str, style: str, label: str, mid_y: float, x1: float, x2: float) -> None:
        st = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=none;startArrow=none;"
              "exitX=0.5;exitY=1;entryX=0.5;entryY=0;labelBackgroundColor=#FFFFFF;fontSize=10;" + style)
        c = ET.SubElement(self.cells, "mxCell", id=self._id("e"), value=label, style=st, edge="1", parent="1",
                          source=src, target=tgt)
        g = ET.SubElement(c, "mxGeometry", relative="1", **{"as": "geometry"})
        pts = ET.SubElement(g, "Array", **{"as": "points"})
        for px in (x1, x2):
            ET.SubElement(pts, "mxPoint", x=str(px), y=str(mid_y))

    # -- layout
    def width(self, name: str) -> int:
        return 10 + LBLW + len(self.cfg.switches[name].ports) * PSTEP + 4

    def layout(self) -> dict[str, tuple[int, int]]:
        levels: dict[int, list[str]] = {}
        for name in sorted(self.cfg.switches, key=lambda n: (self.cfg.depth(n), n)):
            levels.setdefault(self.cfg.depth(name), []).append(name)
        row_w = {d: sum(self.width(n) for n in ns) + SWITCH_GAP * (len(ns) - 1) for d, ns in levels.items()}
        page_w = max(row_w.values())
        top = MARGIN + 130  # room for title, uplink stubs and legend
        pos = {}
        for d, names in levels.items():
            x = MARGIN + 360 + (page_w - row_w[d]) // 2  # legend sits in the left 360px
            for name in names:
                pos[name] = (x, top + d * (self.swh + LEVEL_GAP))
                x += self.width(name) + SWITCH_GAP
        self.page = (MARGIN * 2 + 360 + page_w, top + len(levels) * (self.swh + LEVEL_GAP))
        return pos

    def flagged(self) -> set[tuple[str, int, int]]:
        """(switch, port, vid) cells where the two ends of a cable disagree."""
        out = set()
        for s in self.cfg.switches.values():
            if s.upstream:
                parent, pport = s.upstream
                a, b = self.cfg.switches[parent].ports[pport - 1], s.ports[s.uplink_port - 1]
                for v in self.vids:
                    if mode(a, v) != mode(b, v):
                        out |= {(parent, pport, v), (s.name, s.uplink_port, v)}
        return out

    # -- drawing
    def build(self) -> str:
        cfg, pos, flags = self.cfg, self.layout(), self.flagged()
        title = escape(cfg.title or "Layer 2 / VLAN topology")
        self.box(f'<b style="font-size:20px">{title}</b><br><span style="font-size:11px;color:#555">'
                 f"management VLAN {cfg.management_vlan} &middot; generated from {escape(cfg.path.name)}</span>",
                 MARGIN + 360, MARGIN, 700, 46, TXT + "align=left;verticalAlign=top;")
        for name, (sx, sy) in pos.items():
            self.switch(name, sx, sy, flags)
        for s in cfg.switches.values():
            ux = pos[s.name][0] + 10 + LBLW + (s.uplink_port - 1) * PSTEP
            if s.upstream:
                parent, pport = s.upstream
                px = pos[parent][0] + 10 + LBLW + (pport - 1) * PSTEP
                mid = pos[s.name][1] - LEVEL_GAP / 2 + (sorted(cfg.switches).index(s.name) % 5) * 12  # stagger parallel cables; deterministic
                self.edge(f"{parent}-p{pport}", f"{s.name}-p{s.uplink_port}", TRUNK,
                          self.membership(s.ports[s.uplink_port - 1]), mid, px + PW / 2, ux + PW / 2)
            else:
                stub = self.box("<b>upstream</b>", ux, pos[s.name][1] - 70, PW, 28,
                                "rounded=0;html=1;fillColor=#37474F;fontColor=#FFFFFF;strokeColor=#37474F;fontSize=10;")
                self.edge(stub, f"{s.name}-p{s.uplink_port}", TRUNK, self.membership(s.ports[s.uplink_port - 1]),
                          pos[s.name][1] - 20, ux + PW / 2, ux + PW / 2)
        self.legend()
        self.model.set("pageWidth", str(self.page[0]))
        self.model.set("pageHeight", str(self.page[1]))
        ET.indent(self.root)
        return ET.tostring(self.root, encoding="unicode") + "\n"

    def membership(self, p: PortSpec) -> str:
        parts = [f"U {p.untagged}"] if p.untagged else []
        if p.tagged:
            parts.append("T " + "·".join(str(v) for v in sorted(p.tagged)))
        return " + ".join(parts)

    def switch(self, name: str, sx: int, sy: int, flags: set) -> None:
        cfg, s = self.cfg, self.cfg.switches[name]
        self.box("", sx, sy, self.width(name), self.swh,
                 "rounded=1;arcSize=3;whiteSpace=wrap;html=1;fillColor=#FAFAFA;strokeColor=#546E7A;strokeWidth=2;")
        self.box(f'<b style="font-size:14px">{escape(name)}</b> <span style="color:#555;font-size:10px">&nbsp;'
                 f"{escape(s.model)} &middot; {escape(s.host)}</span>",
                 sx + 10, sy + 4, self.width(name) - 20, 36, TXT + "align=left;verticalAlign=middle;fontSize=10;")
        cy = sy + TOP
        self.box("<b>Port</b>", sx + 10, cy, LBLW - 4, HDR, TXT + "align=right;verticalAlign=middle;fontSize=10;spacingRight=4;")
        for i, v in enumerate(self.vids):
            c = self.color[v]
            self.box(f"<b>{v}</b> {escape(cfg.vlans[v].name)}", sx + 10, cy + HDR + i * ROWH, LBLW - 4, ROWH,
                     f"rounded=0;html=1;whiteSpace=wrap;align=left;spacingLeft=6;fontSize=9;fillColor={tint(c, 0.18)};"
                     f"fontColor=#212121;shape=partialRectangle;right=0;top=0;bottom=0;left=1;strokeColor={c};strokeWidth=4;")
        self.box("<b>PVID</b>", sx + 10, cy + HDR + len(self.vids) * ROWH, LBLW - 4, PVH,
                 TXT + "align=right;verticalAlign=middle;fontSize=10;spacingRight=4;")
        for p in s.ports:
            x = sx + 10 + LBLW + (p.number - 1) * PSTEP
            self.box(f'<b style="font-size:12px">{p.number}</b><br><span style="font-size:8px">{escape(p.name) or "&nbsp;"}</span>',
                     x, cy, PW, HDR, "rounded=0;html=1;whiteSpace=wrap;fillColor=#37474F;fontColor=#FFFFFF;"
                                     "strokeColor=#37474F;verticalAlign=middle;spacing=0;")
            for i, v in enumerate(self.vids):
                c, k = self.color[v], mode(p, v)
                if k == "U":
                    st = f"fillColor={c};fontColor=#FFFFFF;fontStyle=1;strokeColor=#FFFFFF;"
                elif k == "T":
                    st = f"fillColor={tint(c, 0.30)};fontColor={c};fontStyle=1;strokeColor=#FFFFFF;"
                else:
                    st = "fillColor=#FFFFFF;strokeColor=#ECEFF1;"
                if (name, p.number, v) in flags:
                    st += "strokeColor=#D50000;strokeWidth=3;dashed=1;"
                self.box(k or "", x, cy + HDR + i * ROWH, PW, ROWH,
                         "rounded=0;html=1;fontSize=10;align=center;verticalAlign=middle;" + st)
            pv_color = self.color.get(p.pvid, "#9E9E9E")
            self.box(str(p.pvid), x, cy + HDR + len(self.vids) * ROWH, PW, PVH,
                     f"rounded=0;html=1;fontSize=10;fontStyle=1;fillColor={tint(pv_color, 0.45)};fontColor=#212121;strokeColor=#FFFFFF;")
            self.box("", x, cy, PW, self.colh, "rounded=0;html=1;fillColor=none;strokeColor=#37474F;strokeWidth=1.5;",
                     id=f"{name}-p{p.number}")

    def legend(self) -> None:
        lx, ly = MARGIN, MARGIN + 60
        rows = len(self.vids)
        self.box("", lx, ly, 330, 150 + rows * 20, "rounded=1;arcSize=3;html=1;fillColor=#FFFFFF;strokeColor=#B0BEC5;")
        self.box("<b>Legend</b>", lx + 12, ly + 6, 200, 22, TXT + "align=left;fontSize=12;")
        for i, v in enumerate(self.vids):
            y = ly + 32 + i * 20
            desc = self.cfg.vlans[v].description
            self.box("", lx + 14, y + 3, 26, 14, f"rounded=0;html=1;fillColor={self.color[v]};strokeColor=none;")
            self.box(f"<b>{v}</b> &nbsp;{escape(self.cfg.vlans[v].name)}"
                     + (f' <span style="color:#555">&mdash; {escape(desc)}</span>' if desc else ""),
                     lx + 48, y, 270, 20, TXT + "align=left;verticalAlign=middle;fontSize=10;")
        y = ly + 38 + rows * 20
        mc = self.color[self.cfg.management_vlan]
        for i, (sym, style, text) in enumerate((
            ("U", f"fillColor={mc};fontColor=#FFFFFF;", "untagged member (frames leave without tag)"),
            ("T", f"fillColor={tint(mc, 0.3)};fontColor={mc};", "tagged member (802.1Q tag kept)"),
            ("", "fillColor=#FFFFFF;strokeColor=#D50000;strokeWidth=3;dashed=1;", "cable ends disagree on this VLAN"),
            (str(self.cfg.management_vlan), f"fillColor={tint(mc, 0.45)};", "PVID: VLAN for untagged frames coming in"),
        )):
            self.box(sym, lx + 14, y + i * 22, 26, 18, f"rounded=0;html=1;fontStyle=1;strokeColor=none;fontSize=10;{style}")
            self.box(text, lx + 48, y + i * 22, 270, 18, TXT + "align=left;verticalAlign=middle;fontSize=10;")
