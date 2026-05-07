"""Generate Agora leadership slide deck as a PDF.

Usage:
    python scripts/build_deck.py [output_path]
"""

from __future__ import annotations

import sys

from reportlab.pdfgen import canvas as rl_canvas

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

W, H = 10 * 72, 7.5 * 72  # 10" x 7.5" landscape slide

# Colors (R, G, B in 0..1)
NAVY      = (0.106, 0.165, 0.290)   # #1B2A4A  header bar
BLUE      = (0.145, 0.388, 0.922)   # #2563EB  accent / bullets
LIGHTBLUE = (0.937, 0.965, 1.000)   # #EFF6FF  table header row
TEAL      = (0.043, 0.510, 0.475)   # #0B8278  shipped / green accent
DARKTEXT  = (0.067, 0.094, 0.153)   # #111827
MIDGRAY   = (0.420, 0.447, 0.502)   # #6B7280
LIGHTGRAY = (0.937, 0.941, 0.949)   # #EFF0F2  alt table row
WHITE     = (1.0, 1.0, 1.0)
ORANGE    = (0.855, 0.380, 0.020)   # #DA6105  risk / gap

HEADER_H   = 70
FOOTER_H   = 22
MARGIN_L   = 40
MARGIN_R   = 40
CONTENT_Y_TOP = H - HEADER_H - 18   # first line of content


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def set_fill(c: rl_canvas.Canvas, rgb: tuple[float, float, float]) -> None:
    c.setFillColorRGB(*rgb)


def set_stroke(c: rl_canvas.Canvas, rgb: tuple[float, float, float]) -> None:
    c.setStrokeColorRGB(*rgb)


def header(c: rl_canvas.Canvas, title: str, subtitle: str = "") -> None:
    """Draw the top navy header bar with slide title."""
    set_fill(c, NAVY)
    c.rect(0, H - HEADER_H, W, HEADER_H, fill=1, stroke=0)

    # Title text
    c.setFont("Helvetica-Bold", 22)
    set_fill(c, WHITE)
    c.drawString(MARGIN_L, H - 44, title)

    if subtitle:
        c.setFont("Helvetica", 11)
        set_fill(c, (0.80, 0.87, 0.97))
        c.drawString(MARGIN_L, H - 62, subtitle)


def footer(c: rl_canvas.Canvas, page_num: int, total: int) -> None:
    """Draw bottom footer with page number and product name."""
    set_fill(c, (0.93, 0.94, 0.96))
    c.rect(0, 0, W, FOOTER_H, fill=1, stroke=0)
    c.setFont("Helvetica", 8)
    set_fill(c, MIDGRAY)
    c.drawString(MARGIN_L, 7, "Agora  |  Agentic Inter-Library Loan System  |  Research Prototype 2026")
    c.drawRightString(W - MARGIN_R, 7, f"{page_num} / {total}")


def section_label(c: rl_canvas.Canvas, text: str, y: float) -> None:
    """Small ALLCAPS blue label above a content block."""
    c.setFont("Helvetica-Bold", 7)
    set_fill(c, BLUE)
    c.drawString(MARGIN_L, y, text.upper())


def h2(c: rl_canvas.Canvas, text: str, y: float, color: tuple = DARKTEXT) -> float:
    """Bold section heading. Returns y after the line."""
    c.setFont("Helvetica-Bold", 14)
    set_fill(c, color)
    c.drawString(MARGIN_L, y, text)
    return y - 18


def bullet(
    c: rl_canvas.Canvas,
    text: str,
    y: float,
    indent: int = 0,
    size: int = 11,
    color: tuple = DARKTEXT,
    dot_color: tuple = BLUE,
    dot: str = "•",
) -> float:
    """Draw a bullet line. Returns y after the line."""
    x = MARGIN_L + indent
    dot_size = size - 1
    c.setFont("Helvetica-Bold", dot_size)
    set_fill(c, dot_color)
    c.drawString(x, y, dot)
    c.setFont("Helvetica", size)
    set_fill(c, color)
    c.drawString(x + 12, y, text)
    return y - (size + 4)


def sub_bullet(c: rl_canvas.Canvas, text: str, y: float, size: int = 10) -> float:
    return bullet(c, text, y, indent=18, size=size, dot_color=MIDGRAY, dot="-")


def body(c: rl_canvas.Canvas, text: str, y: float, size: int = 11,
         color: tuple = DARKTEXT, max_width: float = W - MARGIN_L - MARGIN_R) -> float:
    """Simple body text line (no wrap). Returns y after."""
    c.setFont("Helvetica", size)
    set_fill(c, color)
    # Naive word-wrap
    words = text.split()
    line = ""
    lines_out = []
    for word in words:
        test = (line + " " + word).strip()
        if c.stringWidth(test, "Helvetica", size) <= max_width:
            line = test
        else:
            lines_out.append(line)
            line = word
    if line:
        lines_out.append(line)
    for ln in lines_out:
        c.drawString(MARGIN_L, y, ln)
        y -= size + 4
    return y


def divider(
    c: rl_canvas.Canvas,
    y: float,
    color: tuple = LIGHTGRAY,
    width_frac: float = 1.0,
) -> float:
    set_stroke(c, color)
    c.setLineWidth(0.5)
    end_x = MARGIN_L + width_frac * (W - MARGIN_L - MARGIN_R)
    c.line(MARGIN_L, y, end_x, y)
    return y - 6


def badge(
    c: rl_canvas.Canvas,
    text: str,
    x: float,
    y: float,
    bg: tuple = TEAL,
    fg: tuple = WHITE,
    size: int = 9,
) -> float:
    """Draw a small filled badge."""
    w = c.stringWidth(text, "Helvetica-Bold", size) + 12
    h_badge = size + 6
    set_fill(c, bg)
    c.roundRect(x, y - 2, w, h_badge, 3, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", size)
    set_fill(c, fg)
    c.drawString(x + 6, y + 2, text)
    return x + w + 6


def kv_row(
    c: rl_canvas.Canvas,
    key: str,
    value: str,
    y: float,
    col1_x: float,
    col2_x: float,
    alt: bool = False,
    row_h: float = 18,
) -> float:
    """One key-value table row."""
    if alt:
        set_fill(c, LIGHTGRAY)
        c.rect(MARGIN_L, y - 4, W - MARGIN_L - MARGIN_R, row_h, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 10)
    set_fill(c, DARKTEXT)
    c.drawString(col1_x, y, key)
    c.setFont("Helvetica", 10)
    set_fill(c, DARKTEXT)
    c.drawString(col2_x, y, value)
    return y - row_h


def table_header_row(
    c: rl_canvas.Canvas,
    cols: list[tuple[str, float]],  # (label, x)
    y: float,
    row_h: float = 20,
) -> float:
    set_fill(c, NAVY)
    c.rect(MARGIN_L, y - 4, W - MARGIN_L - MARGIN_R, row_h, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 10)
    set_fill(c, WHITE)
    for label, x in cols:
        c.drawString(x, y + 3, label)
    return y - row_h


def table_row(
    c: rl_canvas.Canvas,
    cells: list[tuple[str, float]],   # (text, x)
    y: float,
    alt: bool = False,
    row_h: float = 18,
    size: int = 10,
) -> float:
    if alt:
        set_fill(c, LIGHTGRAY)
        c.rect(MARGIN_L, y - 4, W - MARGIN_L - MARGIN_R, row_h, fill=1, stroke=0)
    c.setFont("Helvetica", size)
    set_fill(c, DARKTEXT)
    for text, x in cells:
        c.drawString(x, y, text)
    return y - row_h


def _draw_check(c: rl_canvas.Canvas, x: float, y: float, color: tuple, size: int = 11) -> None:
    """Draw a tick mark using two line segments (no Unicode font dependency)."""
    set_stroke(c, color)
    c.setLineWidth(1.6)
    s = size * 0.45
    # short stroke (down-right) then long stroke (up-right) — classic check shape
    c.line(x, y + s * 0.55, x + s * 0.55, y)
    c.line(x + s * 0.55, y, x + s * 1.55, y + s * 1.4)


def checkmark(c: rl_canvas.Canvas, text: str, y: float, size: int = 11) -> float:
    _draw_check(c, MARGIN_L, y + 1, TEAL, size=size)
    c.setFont("Helvetica", size)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L + 14, y, text)
    return y - (size + 4)


def gap_item(c: rl_canvas.Canvas, text: str, y: float, size: int = 11) -> float:
    c.setFont("Helvetica-Bold", size)
    set_fill(c, ORANGE)
    c.drawString(MARGIN_L, y, "!")
    c.setFont("Helvetica", size)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L + 14, y, text)
    return y - (size + 4)


# ---------------------------------------------------------------------------
# Slide builders
# ---------------------------------------------------------------------------

def slide_title(c: rl_canvas.Canvas) -> None:
    # Full-bleed navy background
    set_fill(c, NAVY)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # Blue accent bar
    set_fill(c, BLUE)
    c.rect(0, H / 2 - 2, W, 4, fill=1, stroke=0)

    # Product name
    c.setFont("Helvetica-Bold", 36)
    set_fill(c, WHITE)
    c.drawCentredString(W / 2, H / 2 + 60, "Agora")

    # Tagline
    c.setFont("Helvetica", 18)
    set_fill(c, (0.75, 0.85, 0.97))
    c.drawCentredString(W / 2, H / 2 + 22, "Agentic Inter-Library Loan System")

    # Subtitle
    c.setFont("Helvetica", 13)
    set_fill(c, (0.60, 0.72, 0.90))
    c.drawCentredString(W / 2, H / 2 - 20, "Functional & Technical Overview  |  Product & Engineering Leadership")

    # Date/version
    c.setFont("Helvetica", 10)
    set_fill(c, (0.45, 0.58, 0.78))
    c.drawCentredString(W / 2, 40, "Research Prototype  |  2026")


def slide_problem(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "The Problem", "Why ILL needs agentic automation")
    footer(c, pn, total)
    y = CONTENT_Y_TOP

    # Left column (60%)
    col_w = 0.58 * (W - MARGIN_L - MARGIN_R)

    section_label(c, "Baseline impact", y + 8)
    y -= 8

    c.setFont("Helvetica-Bold", 13)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L, y, "NCIP adoption alone reduces staff steps:")
    y -= 18

    # Stat boxes — two side-by-side pills
    stat_boxes = [
        ("~50%", "Borrow-side", BLUE),
        ("~42%", "Lend-side",   TEAL),
    ]
    bw, bh = 130, 44
    for idx, (pct, label, color) in enumerate(stat_boxes):
        bx = MARGIN_L + idx * (bw + 10)
        by = y - 32
        set_fill(c, color)
        c.roundRect(bx, by, bw, bh, 6, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 22)
        set_fill(c, WHITE)
        c.drawCentredString(bx + bw / 2, by + 20, pct)
        c.setFont("Helvetica", 10)
        c.drawCentredString(bx + bw / 2, by + 7, label)

    y -= 42
    c.setFont("Helvetica", 9)
    set_fill(c, MIDGRAY)
    c.drawString(MARGIN_L, y, "Source: NISO benchmarks")
    y -= 30

    section_label(c, "What humans still do manually", y + 8)
    y -= 8
    for item in [
        "Discovery & supplier ranking",
        "Copyright clearance (CONTU rule tracking)",
        "Status chasing across peer libraries",
        "Recall coordination & return logistics",
        "Reconciliation of billing / SLA discrepancies",
    ]:
        y = bullet(c, item, y, size=11)

    y -= 10
    divider(c, y + 6, width_frac=0.58)
    c.setFont("Helvetica-BoldOblique", 10)
    set_fill(c, NAVY)
    # Wrap to two lines so it fits in the 58 % left column.
    c.drawString(MARGIN_L, y - 8,
        "ILL spans heterogeneous systems, long timelines, real money,")
    c.drawString(MARGIN_L, y - 22,
        "and legal compliance (CONTU).")

    # Right callout box
    rx = MARGIN_L + col_w + 20
    rw = W - rx - MARGIN_R
    ry = CONTENT_Y_TOP - 30
    rh = 200
    set_fill(c, LIGHTBLUE)
    c.roundRect(rx, ry - rh, rw, rh, 8, fill=1, stroke=0)
    set_stroke(c, BLUE)
    c.setLineWidth(1.5)
    c.roundRect(rx, ry - rh, rw, rh, 8, fill=0, stroke=1)
    c.setFont("Helvetica-Bold", 11)
    set_fill(c, NAVY)
    c.drawCentredString(rx + rw / 2, ry - 18, "Complexity drivers")
    divider(c, ry - 26, BLUE)
    cy = ry - 44
    for item in [
        "Multi-party, multi-system",
        "Frequent failures (unavailable,",
        "  declined, lost in transit)",
        "Long-running (days to weeks)",
        "Legal compliance (CONTU)",
        "Real financial exposure",
    ]:
        c.setFont("Helvetica", 10)
        set_fill(c, DARKTEXT)
        c.drawString(rx + 12, cy, item)
        cy -= 16


def slide_hypothesis(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Hypothesis", "What Agora is testing")
    footer(c, pn, total)
    y = CONTENT_Y_TOP

    c.setFont("Helvetica-Bold", 13)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L, y,
        "A multi-agent orchestrator over standards-compliant ILL infrastructure can:")
    y -= 26

    items = [
        (BLUE,   "Compress the human-touch surface further",
                 "by automating discovery, routing, copyright checks, and status tracking"),
        (TEAL,   "Improve correctness",
                 "via explicit saga + compensator semantics for every state transition"),
        (NAVY,   "Maintain legal and policy safety",
                 "by keeping humans in the loop — agents advise, staff commit"),
    ]
    for color, headline, detail in items:
        # Numbered chip
        set_fill(c, color)
        c.circle(MARGIN_L + 10, y + 4, 10, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 10)
        set_fill(c, WHITE)
        c.drawCentredString(MARGIN_L + 10, y + 1, str(items.index((color, headline, detail)) + 1))
        c.setFont("Helvetica-Bold", 13)
        set_fill(c, color)
        c.drawString(MARGIN_L + 26, y + 4, headline)
        c.setFont("Helvetica", 11)
        set_fill(c, MIDGRAY)
        c.drawString(MARGIN_L + 26, y - 10, detail)
        y -= 46

    y -= 10
    divider(c, y)
    y -= 22

    section_label(c, "Scope", y + 8)
    y -= 8

    c.setFont("Helvetica-Bold", 11)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L, y, "In scope for this prototype:")
    y -= 16
    for item in [
        "End-to-end lifecycle with two simulated tenants (real ReShare sandbox)",
        "Saga compensation correctness under arbitrary forward sequences",
        "Idempotency: replay any message N times, observable effect once",
        "Agent reasoning traces visible to staff in the console",
    ]:
        y = checkmark(c, item, y, size=11)

    y -= 10
    c.setFont("Helvetica-Bold", 11)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L, y, "Explicitly out of scope:")
    y -= 16
    for item in [
        "Production deployment, FedRAMP authorization, real billing, patron-facing UI",
    ]:
        y = bullet(c, item, y, size=11, dot_color=MIDGRAY, dot="-")


def slide_lifecycle(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Lifecycle & State Machine", "Human approval at every transition")
    footer(c, pn, total)
    y = CONTENT_Y_TOP

    # Draw the lifecycle flow diagram
    states = [
        ("SUBMITTED", NAVY),
        ("ROUTED",    BLUE),
        ("APPROVING", (0.40, 0.28, 0.78)),  # purple waypoint
        ("APPROVED",  TEAL),
        ("SHIPPED",   (0.145, 0.55, 0.388)),
        ("RECEIVED",  (0.60, 0.36, 0.027)),
        ("RETURNED",  TEAL),
    ]
    # Place states in two rows for readability
    row1 = states[:4]
    row2 = states[4:]

    box_w, box_h = 112, 32
    gap = 18
    total_w1 = len(row1) * box_w + (len(row1) - 1) * gap
    start_x1 = (W - total_w1) / 2

    def draw_state_row(row: list, sy: float, start_x: float) -> None:
        for i, (name, col) in enumerate(row):
            bx = start_x + i * (box_w + gap)
            set_fill(c, col)
            c.roundRect(bx, sy - box_h, box_w, box_h, 5, fill=1, stroke=0)
            c.setFont("Helvetica-Bold", 9)
            set_fill(c, WHITE)
            c.drawCentredString(bx + box_w / 2, sy - box_h / 2 - 4, name)
            # Arrow to next
            if i < len(row) - 1:
                ax = bx + box_w + 2
                ay = sy - box_h / 2
                set_stroke(c, MIDGRAY)
                c.setLineWidth(1.2)
                c.line(ax, ay, ax + gap - 4, ay)
                # Arrowhead
                c.line(ax + gap - 4, ay, ax + gap - 10, ay + 4)
                c.line(ax + gap - 4, ay, ax + gap - 10, ay - 4)

    sy1 = y - 10
    draw_state_row(row1, sy1, start_x1)

    # Bend arrow down from APPROVED to SHIPPED
    last_r1_x = start_x1 + (len(row1) - 1) * (box_w + gap)
    total_w2 = len(row2) * box_w + (len(row2) - 1) * gap
    start_x2 = (W - total_w2) / 2
    sy2 = sy1 - box_h - 40

    # Down arrow from APPROVED box centre to row2 level
    approved_cx = last_r1_x + box_w / 2
    set_stroke(c, MIDGRAY)
    c.setLineWidth(1.2)
    c.line(approved_cx, sy1 - box_h, approved_cx, sy2 + 2)
    c.line(approved_cx, sy2 + 2, approved_cx - 4, sy2 + 8)
    c.line(approved_cx, sy2 + 2, approved_cx + 4, sy2 + 8)

    draw_state_row(row2, sy2, start_x2)

    # Terminal states row
    sy3 = sy2 - box_h - 30
    terminals = [
        ("CANCELLED",  (0.72, 0.16, 0.16)),
        ("UNFILLED",   (0.80, 0.45, 0.10)),
        ("DISPUTED",   (0.50, 0.50, 0.50)),
    ]
    total_w3 = len(terminals) * box_w + (len(terminals) - 1) * gap
    start_x3 = (W - total_w3) / 2
    c.setFont("Helvetica-Bold", 9)
    set_fill(c, MIDGRAY)
    c.drawCentredString(W / 2, sy3 + 14, "Terminal states (compensator paths)")
    for i, (name, col) in enumerate(terminals):
        bx = start_x3 + i * (box_w + gap)
        set_fill(c, col)
        c.roundRect(bx, sy3 - box_h, box_w, box_h, 5, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 9)
        set_fill(c, WHITE)
        c.drawCentredString(bx + box_w / 2, sy3 - box_h / 2 - 4, name)

    # APPROVING annotation
    approving_x = start_x1 + 2 * (box_w + gap)
    c.setFont("Helvetica-Oblique", 9)
    set_fill(c, (0.40, 0.28, 0.78))
    c.drawString(approving_x, sy1 - box_h - 12, "Waypoint: awaiting supplier ACK via outbox")

    # Key principles row at bottom
    y_bottom = sy3 - box_h - 22
    divider(c, y_bottom)
    y_bottom -= 14
    principles = [
        ("Compensator paired to every forward step", BLUE),
        ("Human commits every gate; agents advise", TEAL),
        ("Append-only saga ledger is source of truth", NAVY),
    ]
    col_w = (W - MARGIN_L - MARGIN_R) / len(principles)
    for i, (text, col) in enumerate(principles):
        px = MARGIN_L + i * col_w
        c.setFont("Helvetica-Bold", 9)
        set_fill(c, col)
        c.drawString(px, y_bottom, "  " + text)


def slide_agents(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Agent Layer", "Six advisory agents — recommendations only, no auto-commit")
    footer(c, pn, total)
    y = CONTENT_Y_TOP + 4

    agents = [
        ("DiscoveryAgent",
         "DOI -> CrossRef (bib identity) -> SRU (holdings). Consortium fallback when SRU has no 852 holdings.",
         BLUE),
        ("RoutingAgent",
         "Ranks suppliers: rules-based weighted sum + optional LLM tie-breaker (Gemini Flash; 19/20 top-1 on eval).",
         NAVY),
        ("PolicyAgent",
         "CONTU copyright check, patron eligibility, hard/soft flag matrix.",
         (0.40, 0.28, 0.78)),
        ("TransactionAgent",
         "Drives ReShare via outbox intents. Submit, confirm shipment, confirm return, cancel request.",
         TEAL),
        ("TrackingAgent + OverdueScanner",
         "3-tier advisory emission: overdue | recall-proposed | receipt-unconfirmed. Runs from FastAPI lifespan.",
         (0.60, 0.36, 0.027)),
        ("ReconciliationAgent",
         "Thin wrapper over Coordinator.run_compensator. Routes to the right compensator given failure context.",
         (0.55, 0.18, 0.18)),
    ]
    row_h = 44
    for i, (name, desc, col) in enumerate(agents):
        ay = y - i * row_h
        if i % 2 == 1:
            # Background rect hugs content (chip top → rationale baseline + 4 padding)
            set_fill(c, (0.975, 0.978, 0.984))
            c.rect(MARGIN_L - 4, ay - 22, W - MARGIN_L - MARGIN_R + 8, 38, fill=1, stroke=0)

        # Color chip
        set_fill(c, col)
        c.roundRect(MARGIN_L, ay - 2, 4, 18, 2, fill=1, stroke=0)

        c.setFont("Helvetica-Bold", 12)
        set_fill(c, col)
        c.drawString(MARGIN_L + 12, ay, name)

        c.setFont("Helvetica", 10)
        set_fill(c, DARKTEXT)
        c.drawString(MARGIN_L + 12, ay - 14, desc)

    y_bottom = y - len(agents) * row_h - 8
    divider(c, y_bottom)
    y_bottom -= 14
    c.setFont("Helvetica-BoldOblique", 11)
    set_fill(c, NAVY)
    c.drawString(MARGIN_L, y_bottom,
        "Invariant: agents produce recommendation + rationale. Staff click commits the gate.")


def slide_architecture(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Architecture", "Layer cake — async, outbox-backed, multi-worker safe")
    footer(c, pn, total)
    y = CONTENT_Y_TOP + 4

    # Draw layer cake
    layers = [
        ("Staff Console",
         "FastAPI  |  HTMX + Jinja2  |  Saga list / detail / browser / approve / compensate",
         NAVY),
        ("Agent Layer",
         "Discovery  |  Routing  |  Policy  |  Transaction  |  Tracking  |  Reconciliation",
         BLUE),
        ("Saga Core",
         "Coordinator  |  SagaLedger (savepoint dedup)  |  StepRegistry  |  OutboxWorker",
         (0.40, 0.28, 0.78)),
        ("Standards Clients",
         "HttpReShareClient  |  HttpNcipClient  |  SruClient  |  CrossrefClient  |  OpenURL parser",
         TEAL),
        ("Persistence",
         "Postgres: saga  |  saga_event  |  outbox (FOR UPDATE SKIP LOCKED)  |  inbox",
         (0.18, 0.35, 0.18)),
    ]

    lh = 52
    # Reserve right-side strip for the "Key invariants" callout (drawn below).
    callout_w = 140
    callout_gap = 18
    callout_x = W - MARGIN_R - callout_w - 5  # 5pt right-edge gutter
    cake_right = callout_x - callout_gap
    for i, (name, detail, col) in enumerate(layers):
        ly = y - i * lh
        # Gradient-style: lighten fill based on depth
        alpha = 1.0 - i * 0.06
        set_fill(c, tuple(v * alpha + (1 - alpha) for v in col))
        bar_x = MARGIN_L + i * 14
        bar_w = cake_right - bar_x - i * 14
        c.roundRect(bar_x, ly - lh + 10, bar_w, lh - 4, 6, fill=1, stroke=0)
        # Label
        c.setFont("Helvetica-Bold", 12)
        set_fill(c, WHITE if i < 3 else DARKTEXT)
        c.drawString(bar_x + 12, ly - 10, name)
        c.setFont("Helvetica", 10)
        set_fill(c, WHITE if i < 3 else DARKTEXT)
        c.drawString(bar_x + 12, ly - 24, detail)

        # Down arrow between layers
        if i < len(layers) - 1:
            ax = bar_x + bar_w / 2
            set_stroke(c, MIDGRAY)
            c.setLineWidth(0.8)
            c.line(ax, ly - lh + 10, ax, ly - lh + 4)

    # Right-side callout (callout_x / callout_w defined above so the
    # cake bars can stop short of it).
    ry_top = y - 2
    ry_bot = y - len(layers) * lh - 4
    rh = ry_top - ry_bot
    set_fill(c, LIGHTBLUE)
    c.roundRect(callout_x, ry_bot, callout_w, rh, 6, fill=1, stroke=0)
    set_stroke(c, BLUE)
    c.setLineWidth(1)
    c.roundRect(callout_x, ry_bot, callout_w, rh, 6, fill=0, stroke=1)

    cy = ry_top - 18
    c.setFont("Helvetica-Bold", 9)
    set_fill(c, NAVY)
    c.drawCentredString(callout_x + callout_w / 2, cy, "Key invariants")
    cy -= 16
    for txt in [
        "Outbox decouples wire calls",
        "from saga tx boundary",
        "",
        "UNIQUE idempotency_key",
        "on ledger + outbox rows",
        "",
        "Savepoint on every append:",
        "replay returns prior row",
        "",
        "Multi-worker safe on Postgres",
        "via SKIP LOCKED + lease",
    ]:
        c.setFont("Helvetica", 9)
        set_fill(c, DARKTEXT)
        c.drawString(callout_x + 7, cy, txt)
        cy -= 13


def slide_standards(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Standards & Integrations", "What Agora talks to and how")
    footer(c, pn, total)
    y = CONTENT_Y_TOP - 4

    col1 = MARGIN_L
    col2 = MARGIN_L + 120
    col3 = MARGIN_L + 250
    col4 = MARGIN_L + 460

    y = table_header_row(c,
        [("Standard", col1), ("Role", col2), ("Implementation", col3), ("Status", col4)],
        y)
    y -= 4

    rows = [
        ("ISO 18626:2021",   "Peer-to-peer ILL messaging",      "Delegated to ReShare mod-rs",          "Delegated"),
        ("NCIP / Z39.83",    "Library <-> ILS circulation",      "HttpNcipClient (PR #98/#99)",           "Source-review"),
        ("SRU",              "Catalog holdings discovery",        "HttpSruClient; LoC default",            "Implemented"),
        ("CrossRef REST",    "DOI -> bibliographic identity",    "HttpCrossrefClient (PR #46)",           "Implemented"),
        ("OpenURL / KEV",    "Citation context strings",         "Pure-Python parser",                    "Implemented"),
        ("FOLIO Okapi",      "ReShare tenant auth",              "OkapiAuth token flow (ADR-0013)",       "Implemented"),
        ("Z39.50 binary",    "Legacy catalog discovery",         "Rejected (ADR-0006)",                   "Skipped"),
    ]

    status_colors = {
        "Delegated":     BLUE,
        "Source-review": ORANGE,
        "Implemented":   TEAL,
        "Skipped":       MIDGRAY,
    }

    for i, (std, role, impl, status) in enumerate(rows):
        alt = i % 2 == 1
        if alt:
            set_fill(c, LIGHTGRAY)
            c.rect(MARGIN_L, y - 4, W - MARGIN_L - MARGIN_R, 18, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 10)
        set_fill(c, DARKTEXT)
        c.drawString(col1, y, std)
        c.setFont("Helvetica", 10)
        c.drawString(col2, y, role)
        c.drawString(col3, y, impl)
        # Status badge
        col = status_colors.get(status, MIDGRAY)
        bw = c.stringWidth(status, "Helvetica-Bold", 8) + 10
        set_fill(c, col)
        c.roundRect(col4, y - 2, bw, 14, 3, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 8)
        set_fill(c, WHITE)
        c.drawString(col4 + 5, y + 2, status)
        y -= 20

    y -= 14
    divider(c, y)
    y -= 22
    section_label(c, "Consortium discovery gap", y + 8)
    y -= 8
    c.setFont("Helvetica", 10)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L, y,
        "WorldCat v1 EOL'd Dec 2024. v2 requires paid subscription. Open SRU targets carry bib-only MARCXML (no MARC 852 holdings).")
    y -= 14
    c.drawString(MARGIN_L, y,
        "POC substitute: consortium-member fallback via AGORA_CONSORTIUM_MEMBERS when SRU returns no holdings (PR #100).")


def slide_shipped(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "What's Shipped", "Working prototype — all core paths green")
    footer(c, pn, total)
    y = CONTENT_Y_TOP + 2

    # Two columns; mid is the x-start of the right column
    mid = W / 2 + 10

    # Left column — functional
    section_label(c, "Functional", y + 8)
    y -= 8
    left_items = [
        "make demo — happy path through MockReShareClient",
        "Staff console: saga list, detail, browser, approve, reject, compensate",
        "DiscoveryAgent wired: POST /sagas/{id}/discover",
        "Override endpoint: DISPUTED -> CANCELLED / UNFILLED",
        "RoutingAgent LLM tie-breaker: 19/20 top-1 (20-scenario eval)",
        "3-tier overdue scanner from FastAPI lifespan",
        "NCIP fan-out on RECEIVE and RETURN (fire-and-forget)",
    ]
    y_left = y
    for item in left_items:
        y_left = checkmark(c, item, y_left, size=10)

    # Right column — engineering
    rx = mid
    y_right = y
    # Draw section label at right-column x (section_label always uses MARGIN_L)
    c.setFont("Helvetica-Bold", 7)
    set_fill(c, BLUE)
    c.drawString(rx, y_right + 8, "ENGINEERING")
    y_right -= 8

    right_items = [
        "401 tests: unit + property (Hypothesis) + e2e",
        "+6 postgres-only in CI (Alembic + ORM parity)",
        "Multi-worker outbox: SKIP LOCKED + claim lease",
        "CI: audit / triple-gate / postgres / routing-eval floor",
        "16 ADRs, 7 PRDs, runbook, architecture diagrams",
        "mypy --strict: src/ AND tests/ (76 source files)",
        "Idempotency: ULID keys, UNIQUE on ledger + outbox",
    ]
    for item in right_items:
        _draw_check(c, rx, y_right + 1, TEAL, size=10)
        c.setFont("Helvetica", 10)
        set_fill(c, DARKTEXT)
        c.drawString(rx + 14, y_right, item)
        y_right -= 15

    y_min = min(y_left, y_right) - 10
    divider(c, y_min)
    y_min -= 16

    # Stat badges at bottom
    stats = [
        ("401", "Tests collected"),
        ("390", "Passing locally"),
        ("16",  "ADRs"),
        ("7",   "PRDs"),
        ("4",   "CI workflows"),
    ]
    bx = MARGIN_L
    for val, label in stats:
        bw = 88
        set_fill(c, NAVY)
        c.roundRect(bx, y_min - 32, bw, 40, 5, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 18)
        set_fill(c, WHITE)
        c.drawCentredString(bx + bw / 2, y_min - 10, val)
        c.setFont("Helvetica", 9)
        set_fill(c, (0.70, 0.80, 0.95))
        c.drawCentredString(bx + bw / 2, y_min - 24, label)
        bx += bw + 8


def slide_decisions(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Key Technical Decisions", "Six ADRs that shaped the architecture")
    footer(c, pn, total)
    y = CONTENT_Y_TOP - 4

    decisions = [
        ("ADR-0001", "Wrap FOLIO/ReShare; don't reimplement ISO 18626",
         "mod-rs is production-tested; reimplementing the 2021 schema is yak-shaving"),
        ("ADR-0002", "Event-sourced saga ledger as source of truth",
         "Replay safety, audit trail, and compensator correctness all require append-only events"),
        ("ADR-0005", "Human approval at every transition (default-deny autonomy)",
         "Legal/policy liability stays with consortium staff even when agents are right"),
        ("ADR-0010", "Saga coordinator, not a state-machine engine",
         "Explicit > implicit. One method per concern. Trivially restart/replayable"),
        ("ADR-0011/12", "Outbox commit-then-enqueue; APPROVING waypoint for APPROVE",
         "Decouples wire failures from saga tx; supplier ACK lands as a projection callback"),
        ("ADR-0014", "LLM tie-breaker: rules-first, LLM only on near-ties",
         "Default-off seam; rules pick is the bulk decision; LLM fires only when score gap <= 0.03"),
    ]

    row_h = 44
    for i, (adr, headline, rationale) in enumerate(decisions):
        dy = y - i * row_h
        if i % 2 == 1:
            # Background rect hugs content (chip top → rationale baseline + padding)
            set_fill(c, (0.975, 0.978, 0.984))
            c.rect(MARGIN_L - 4, dy - 22, W - MARGIN_L - MARGIN_R + 8, 38, fill=1, stroke=0)
        # ADR chip
        cw = c.stringWidth(adr, "Helvetica-Bold", 9) + 12
        set_fill(c, NAVY)
        c.roundRect(MARGIN_L, dy - 2, cw, 16, 3, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 9)
        set_fill(c, WHITE)
        c.drawString(MARGIN_L + 6, dy + 2, adr)
        # Headline
        c.setFont("Helvetica-Bold", 12)
        set_fill(c, DARKTEXT)
        c.drawString(MARGIN_L + cw + 10, dy, headline)
        # Rationale
        c.setFont("Helvetica-Oblique", 10)
        set_fill(c, MIDGRAY)
        c.drawString(MARGIN_L + cw + 10, dy - 14, rationale)

    y_bottom = y - len(decisions) * row_h - 8
    divider(c, y_bottom)
    y_bottom -= 14
    c.setFont("Helvetica", 10)
    set_fill(c, MIDGRAY)
    c.drawString(MARGIN_L, y_bottom,
        "Full ADR record: docs/adr/0001-0016. Each covers Status / Context / Decision / Consequences.")


def slide_gaps(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Remaining Gaps & Risks", "Known-gap inventory — prototype scope")
    footer(c, pn, total)
    y = CONTENT_Y_TOP + 2

    # Two columns
    mid = int(W * 0.52)
    rx = mid + 20

    section_label(c, "Sandbox-blocked (external dependency needed)", y + 8)
    y -= 8
    sandbox = [
        ("NCIP live probe",
         "HttpNcipClient source-review-only; test harness ready (test_ncip_http_smoke.py).",
         "Need real FOLIO tenant with mod-ncip"),
        ("Recall compensator",
         "No requester-initiated recall action in mod-rs Actions.groovy.",
         "ADR needed: ISO 18626 Cancel via 'message' vs manualClose"),
        ("ReShare borrower-tenant",
         "Requester-side REQ_* state flow unconfirmed on real borrower tenant.",
         "Probe confirmed Responder side only"),
    ]
    for title, detail, blocker in sandbox:
        c.setFont("Helvetica-Bold", 10)
        set_fill(c, ORANGE)
        c.drawString(MARGIN_L, y, "! " + title)
        y -= 13
        c.setFont("Helvetica", 9)
        set_fill(c, DARKTEXT)
        c.drawString(MARGIN_L + 10, y, detail)
        y -= 12
        c.setFont("Helvetica-Oblique", 9)
        set_fill(c, MIDGRAY)
        c.drawString(MARGIN_L + 10, y, "Blocker: " + blocker)
        y -= 16

    y -= 8
    section_label(c, "Design decisions deferred", y + 8)
    y -= 8
    deferred = [
        "ReconciliationAgent: thin wrapper; no failure policy",
        "WorldCat: structural gap; consortium fallback in place",
        "No auth on staff console (ADR-0007)",
        "No OpenTelemetry traces (structlog only)",
    ]
    for item in deferred:
        y = bullet(c, item, y, size=10, dot_color=MIDGRAY)

    # Right: explicit non-goals box
    ry_top = CONTENT_Y_TOP + 6
    rh = 200
    set_fill(c, (0.995, 0.975, 0.965))
    c.roundRect(rx, ry_top - rh, W - rx - MARGIN_R, rh, 8, fill=1, stroke=0)
    set_stroke(c, ORANGE)
    c.setLineWidth(1.0)
    c.roundRect(rx, ry_top - rh, W - rx - MARGIN_R, rh, 8, fill=0, stroke=1)
    cy = ry_top - 18
    c.setFont("Helvetica-Bold", 11)
    set_fill(c, ORANGE)
    c.drawCentredString((rx + W - MARGIN_R) / 2, cy, "Explicit Non-Goals")
    cy -= 20
    for item in [
        "Production deployment",
        "FedRAMP authorization",
        "Real money / billing",
        "Patron-facing UI",
        "Multi-region / HA topology",
        "Z39.50 binary protocol",
    ]:
        c.setFont("Helvetica", 10)
        set_fill(c, DARKTEXT)
        c.drawString(rx + 14, cy, item)
        cy -= 16


def slide_summary(c: rl_canvas.Canvas, pn: int, total: int) -> None:
    header(c, "Summary", "What the prototype demonstrates")
    footer(c, pn, total)
    y = CONTENT_Y_TOP + 4

    c.setFont("Helvetica-Bold", 13)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L, y, "Proven by this prototype:")
    y -= 24

    proven = [
        "End-to-end lifecycle (Submit -> Return) through real ReShare sandbox",
        "Saga compensation rolls back correctly under arbitrary forward sequences (Hypothesis property tests)",
        "Idempotency: replay any message N times, observable effect once (ULID UNIQUE on ledger + outbox)",
        "Agent reasoning traces in the staff console drive transparent approvals",
        "Multi-worker outbox safety on Postgres (SKIP LOCKED + orphan lease recovery)",
        "LLM tie-breaker improves routing accuracy: 19/20 top-1 vs rules-only 16/20",
    ]
    for item in proven:
        y = checkmark(c, item, y, size=11)

    y -= 14
    divider(c, y)
    y -= 18

    c.setFont("Helvetica-Bold", 13)
    set_fill(c, DARKTEXT)
    c.drawString(MARGIN_L, y, "Central insight:")
    y -= 18

    # Quote box
    set_fill(c, LIGHTBLUE)
    c.roundRect(MARGIN_L, y - 56, W - MARGIN_L - MARGIN_R, 64, 8, fill=1, stroke=0)
    set_stroke(c, BLUE)
    c.setLineWidth(2)
    c.line(MARGIN_L + 6, y - 56, MARGIN_L + 6, y + 8)
    c.setFont("Helvetica-BoldOblique", 13)
    set_fill(c, NAVY)
    c.drawString(MARGIN_L + 22, y - 10,
        "Human-in-the-loop and agent autonomy are not in tension when the default is deny.")
    c.setFont("Helvetica-Oblique", 11)
    set_fill(c, BLUE)
    c.drawString(MARGIN_L + 22, y - 32,
        "Agents compress the discovery and reasoning burden. Staff retain commit authority.")
    c.setFont("Helvetica-Oblique", 10)
    set_fill(c, MIDGRAY)
    c.drawString(MARGIN_L + 22, y - 50,
        "This separation is the core contribution of Agora's design.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SLIDES = [
    ("title",        slide_title),
    ("problem",      slide_problem),
    ("hypothesis",   slide_hypothesis),
    ("lifecycle",    slide_lifecycle),
    ("agents",       slide_agents),
    ("architecture", slide_architecture),
    ("standards",    slide_standards),
    ("shipped",      slide_shipped),
    ("decisions",    slide_decisions),
    ("gaps",         slide_gaps),
    ("summary",      slide_summary),
]


def build(output_path: str) -> None:
    total = len(SLIDES)
    page_size = (W, H)
    c = rl_canvas.Canvas(output_path, pagesize=page_size)
    c.setTitle("Agora — Functional & Technical Overview")
    c.setAuthor("Agora Research Prototype")
    c.setSubject("Product & Engineering Leadership Deck")

    for i, (name, fn) in enumerate(SLIDES):
        if name == "title":
            fn(c)
        else:
            fn(c, i, total)
        c.showPage()

    c.save()
    print(f"Saved: {output_path}  ({total} slides)")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "artifacts/agora_deck.pdf"
    build(out)
