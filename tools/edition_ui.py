"""Shared rendering for a channel edition: charts, tables and the dashboards.

Everything here is pure -- it takes numbers and returns HTML -- except the three
dashboard builders, which read the JSON their tools emit. It exists so a second channel
can be generated without a second copy of the chart code; the single-channel generator
it replaced (build_ghost_drafts) hardcoded one channel and one quarter, and was removed
once build_edition covered both parametrically.

Every chart is STATIC table HTML with an optional script layer on top, because Ghost
strips <script> and email clients never run JS: stripped of its script an edition
degrades to a long sorted table rather than to nothing.
"""
from __future__ import annotations
import html, json, pathlib, re, sys

import pandas as pd

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from build_descriptions import prettify  # noqa: E402  (shared name-casing rules)
from periods import PERIOD_RE, period_mask, is_month, period_label  # noqa: E402,F401

QUARTER = "2026 Q2"
AS_OF = "28 August 2026"   # the date an edition is read; settles which primaries have run

# The validated categorical pair. NOT the house navy/green, which fails the
# normal-vision separation floor (dE 13.9, needs >= 15) -- readers could not tell
# primary from general apart.
PRIMARY, GENERAL, OTHER, TRACK = "#2a78d6", "#eb6834", "#c8ced4", "#eef0f2"
# Undated money is an absence of information, so it reads as grey; the two greys
# separate on lightness alone, which stays legible under any colour vision.
UNDATED = "#8b95a1"



CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;max-width:820px;margin:0 auto;padding:20px;color:#1a1a1a;line-height:1.6;background:#ffffff;}
h1{color:#1a1a1a;border-bottom:2px solid #e0e0e0;padding-bottom:6px;font-size:1.35em;}
h2{color:#333;font-size:1.08em;}
a{color:#2d5a8e;}
.key-takeaways{background:linear-gradient(135deg,#1e3a5f,#2d5a8e);color:#fff;padding:18px 22px;border-radius:8px;margin-bottom:24px;}
.key-takeaways h2{color:#fff;margin-top:0;}
.context{background:linear-gradient(135deg,#1a3d2e,#2a6f4e);color:#fff;padding:18px 22px;border-radius:8px;margin-bottom:24px;}
.context h2{color:#fff;margin-top:0;}
.ctx-item{margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.2);}
.ctx-item:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0;}
.ctx-item h3{color:#b8e6cc;margin:0 0 4px;font-size:1.03em;}
.ctx-item p{margin:0 0 6px;}
.figure{border:1px solid #e0e0e0;border-radius:8px;padding:16px 18px;margin:12px 0 4px;background:#fcfcfd;}
.figcap{font-size:.8em;color:#6b7683;margin:0 0 12px;letter-spacing:.03em;}
.legend{font-size:.8em;color:#5a6672;margin:10px 0 0;}
"""


def slug(channel: str) -> str:
    """Channel name -> filename stem. Two editions must not overwrite each other's data."""
    return re.sub(r"[^a-z0-9]+", "_", channel.lower()).strip("_")


EDITION_ROOT = REPO / "out" / "ghost"
SIDES = ("SEND", "RECEIVE")


def edition_path(channel: str, period: str, side: str, root=None):
    """The ONE place an edition's filename is formed.

    The builder that writes an edition and the uploader that posts it both come here, so
    the two cannot drift. They did: the uploader carried two literal filenames and went on
    posting one stale channel and quarter long after the builder had been parameterised.
    A name that is spelled out in two files is a name that will disagree with itself.
    """
    side = side.upper()
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    return (pathlib.Path(root) if root else EDITION_ROOT) / \
        f"{slug(channel)}_{slug(period)}_{side}.html"


# Party colour goes on the LETTER only. Colouring a whole name makes a table of 500
# rows read as two blocks of colour and buries the money, which is what the reader is
# actually scanning; a single tinted character is found instantly and costs nothing.
PARTY_COLOUR = {"D": "#1f5fa8", "R": "#c0392b", "I": "#6b7683", "L": "#6b7683"}
_PARTY_RE = re.compile(r"\((D|R|I|L)(-[A-Z]{2}(?:-(?:\d{2}|Sen))?)?\)")


def party_letter(letter: str) -> str:
    c = PARTY_COLOUR.get(str(letter)[:1].upper(), "#6b7683")
    return f'<span style="color:{c};font-weight:600;">{html.escape(str(letter)[:1])}</span>'


def colour_party(escaped: str) -> str:
    """Tint the party letter inside text that has ALREADY been HTML-escaped.

    Runs after escaping so the spans it inserts survive; matches only a parenthesised
    D/R/I/L optionally followed by a seat, which is the one shape these editions use.
    """
    return _PARTY_RE.sub(lambda m: f'({party_letter(m.group(1))}{m.group(2) or ""})', escaped)


# FEC committee names carry a lot of boilerplate ("EMPLOYEES OF NORTHROP GRUMMAN
# CORPORATION POLITICAL ACTION COMMITTEE (ENGPAC)"). The hand-written Finance edition
# shortened these by eye; a generator over nineteen channels needs a rule.
_TAIL = re.compile(
    r"\s*(voluntary political contribution (plan|committee)|political action committee"
    r"|federal political action committee|employees political action committee"
    r"|political contribution plan|federal pac|pac)\b.*$", re.I)
_NOISE = re.compile(r"\b(company|corporation|incorporated|inc|corp|llc|the)\b\.?", re.I)


def short_cmte(name: str, keep: int = 40) -> str:
    n = str(name)
    n = re.sub(r"\s*\([^)]*\)\s*$", "", n)
    n = re.sub(r"^employees of\s+", "", n, flags=re.I)
    n = _TAIL.sub("", n)
    n = _NOISE.sub("", n)
    n = re.sub(r"[-\s]+", " ", n).strip(" -,;")
    n = prettify(n) if n else prettify(str(name))
    # Only re-attach "PAC" where we actually removed one. "Fairshake" is the committee's
    # whole name, and appending PAC to it invents a word the filing does not use.
    had_pac = bool(_TAIL.search(str(name))) or "PAC" in str(name).upper()
    if had_pac and not n.lower().endswith("pac"):
        n = f"{n} PAC"
    return n if len(n) <= keep else n[:keep - 1].rstrip(" ,-") + "\u2026"


# Dashboard rows are ONE LINE each. A wrapped cell doubles a row's height, and over
# 500 rows that is the difference between a table you can scan and one you scroll. Every
# cell is nowrap; the text columns are truncated to a width that fits and carry the full
# string in a title attribute, and the long ones drop a step in size rather than wrap.
ROW = ("padding:3px 10px 3px 0;white-space:nowrap;overflow:hidden;"
       "text-overflow:ellipsis;border-top:1px solid #eef0f2;")
SMALL = "font-size:.93em;"

def money(v):
    a = abs(round(v))
    s = f"${a/1e6:.1f}M" if a >= 1e6 else (f"${a/1e3:.0f}k" if a >= 1000 else f"${a:,.0f}")
    return "-" + s if v < 0 else s

def bars(rows, color):
    """Single-series magnitude bars, email-safe."""
    mx = max(abs(v) for _, v in rows) or 1
    out = ['<table style="width:100%;border-collapse:collapse;font-size:.9em;">']
    for lab, v in rows:
        pct = max(abs(v) / mx * 100, 0.6)
        out.append(
            f'<tr><td style="padding:5px 10px 5px 0;white-space:nowrap;color:#333;width:1%;">{html.escape(lab)}</td>'
            f'<td style="padding:5px 10px 5px 0;"><table style="width:100%;border-collapse:collapse;background:{TRACK};border-radius:0 3px 3px 0;">'
            f'<tr><td style="width:{pct:.1f}%;background:{color};height:10px;line-height:10px;border-radius:0 3px 3px 0;font-size:0;">&nbsp;</td>'
            f'<td style="width:{100-pct:.1f}%;font-size:0;">&nbsp;</td></tr></table></td>'
            f'<td style="padding:5px 0;text-align:right;white-space:nowrap;width:1%;">{money(v)}</td></tr>')
    return "".join(out) + "</table>"

# Time reads left to right. Every month-indexed chart below is drawn as columns with
# the month on the x-axis; the horizontal-bar versions these replaced put time on the
# y-axis, which inverts the one axis convention a reader does not have to be taught.
PLOT_H = 150          # px, the plot area every column is scaled against
GAP = 2               # px of surface between stacked segments and adjacent columns


def _col(segments, height=PLOT_H):
    """One vertical column: a spacer, then coloured bands stacked from the bottom.

    Bands arrive bottom-first and are emitted top-first, because an HTML table grows
    downward while a bar grows upward.
    """
    used = sum(h for h, _ in segments) + GAP * max(len(segments) - 1, 0)
    rows = [f'<tr><td style="height:{max(height - used, 0)}px;font-size:0;line-height:0;">&nbsp;</td></tr>']
    for k, (h, col) in enumerate(reversed(segments)):
        if k:
            rows.append(f'<tr><td style="height:{GAP}px;font-size:0;line-height:0;">&nbsp;</td></tr>')
        rows.append(f'<tr><td style="height:{h}px;background:{col};font-size:0;line-height:0;">&nbsp;</td></tr>')
    return ('<table style="width:100%;border-collapse:collapse;">' + "".join(rows) + "</table>")


def _xaxis(labels, sublabels=None, strong=()):
    cells = ""
    for i, lab in enumerate(labels):
        sub = (sublabels or [""] * len(labels))[i]
        bold = "font-weight:600;" if lab in strong else ""
        cells += (f'<td style="padding:6px 3px 0;text-align:center;border-top:1px solid #d8dde2;'
                  f'font-size:.78em;color:#333;{bold}">{html.escape(lab)}'
                  + (f'<br><span style="color:#8b95a1;font-size:.9em;">{html.escape(sub)}</span>' if sub else "")
                  + "</td>")
    return cells


def _legend(series, colors, tail=""):
    leg = " &nbsp; ".join(
        f'<span style="display:inline-block;width:10px;height:10px;background:{c};"></span> {html.escape(n)}'
        for n, c in zip(series, colors))
    return f'<p class="legend">{leg}{tail}</p>'


def vstack(rows, series, colors, highlight=(), tail="", normalize=False):
    """Stacked columns, one per month, month on the x-axis.

    Series are passed in rather than fixed at primary/general/other because a
    three-bucket split hid a real error: the undated "P" designation was being added
    to primary money, so $27.8M of committee-to-committee transfers was reported as
    primary-season giving. A bucket has to exist before it can be seen.

    `normalize` gives every column the full height and divides it by share. One band
    here is 73% of the quarter, which on an absolute scale crushes the other three
    into slivers -- the chart then shows only the fact that one band is huge, which
    the money total beside it already says. Normalising trades the level (printed
    above each column anyway) for a mix that is legible in every month.
    """
    mx = max(sum(max(v, 0) for v in vals) for _, vals in rows) or 1
    plot, tot = "", ""
    for lab, vals in rows:
        scale = (sum(max(v, 0) for v in vals) or 1) if normalize else mx
        segs = [(max(round(max(v, 0) / scale * PLOT_H), 2), c)
                for v, c in zip(vals, colors) if v > 0]
        plot += f'<td style="vertical-align:bottom;padding:0 3px;">{_col(segs)}</td>'
        bold = "font-weight:600;" if lab in highlight else ""
        tot += (f'<td style="padding:5px 3px 0;text-align:center;font-size:.78em;'
                f'color:#1a1a1a;{bold}">{money(sum(vals))}</td>')
    if normalize:
        tail += (" &nbsp;&mdash;&nbsp; columns are share of the month; the figure above each "
                 "is the month's total.")
    return (f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;">'
            f'<tr>{plot}</tr><tr>{tot}</tr><tr>{_xaxis([r[0] for r in rows], strong=highlight)}</tr></table>'
            + _legend(series, colors, tail))


def vgroup(rows, series, colors, share_label=None):
    """Grouped columns -- the series sit side by side inside each month.

    A stack cannot show a crossover. When two series swap rank inside a quarter the
    column total barely moves while the composition flips, so the reader sees a flat
    chart and misses the whole event. Side by side on a shared scale makes the swap
    the most visible thing on the page, which is what it is.
    """
    mx = max(max(vals) for _, vals in rows) or 1
    plot, val = "", ""
    for lab, vals in rows:
        inner = "".join(
            f'<td style="vertical-align:bottom;padding:0 2px;">'
            f'{_col([(max(round(max(v, 0) / mx * PLOT_H), 1), c)])}</td>'
            for v, c in zip(vals, colors))
        plot += (f'<td style="vertical-align:bottom;padding:0 6px;">'
                 f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;">'
                 f'<tr>{inner}</tr></table></td>')
        share = ""
        if share_label:
            t = sum(abs(v) for v in vals) or 1
            share = (f'<br><span style="color:{colors[-1]};font-weight:600;">'
                     f'{abs(vals[-1]) / t * 100:.0f}%</span>')
        val += (f'<td style="padding:5px 6px 0;text-align:center;font-size:.78em;color:#1a1a1a;">'
                + " &nbsp; ".join(money(v) for v in vals) + share + "</td>")
    tail = (f' &nbsp;&mdash;&nbsp; the percentage is {html.escape(share_label)}'
            if share_label else "")
    return (f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;">'
            f'<tr>{plot}</tr><tr>{val}</tr><tr>{_xaxis([r[0] for r in rows])}</tr></table>'
            + _legend(series, colors, tail))


def vtrend(rows, color=PRIMARY, mark=GENERAL, label_every=(), average=True):
    """Monthly columns, optionally carrying a tick at their own trailing 12-month average.

    A quarterly newsletter reads one quarter at a time, which makes it impossible to
    tell an unusually large month from a channel that has simply grown. Putting the
    trailing average on the same column answers that in place, and because the tick
    moves month to month the ticks together read as a stepped average line.

    The tick is drawn by SPLITTING the column at the average rather than overlaying
    it, since an email-safe table cannot stack elements.

    average=False drops the tick and leaves a plain twelve-month history. The legend
    drops with it -- naming a series the chart no longer draws is worse than no legend.
    """
    mx = max(v for _, v, _ in rows) or 1
    plot, val = "", ""
    for lab, v, avg in rows:
        h = max(round(max(v, 0) / mx * PLOT_H), 1)
        if avg is None or not average:
            segs = [(h, color)]
        else:
            a = max(round(avg / mx * PLOT_H), 1)
            if a < h:                       # tick sits inside the column
                segs = [(a, color), (3, mark), (max(h - a - 3, 1), color)]
            else:                           # month came in under its average
                segs = [(h, color), (max(a - h - 3, 0), TRACK), (3, mark)]
        plot += f'<td style="vertical-align:bottom;padding:0 3px;">{_col(segs)}</td>'
        show = (not label_every) or lab in label_every
        val += (f'<td style="padding:5px 2px 0;text-align:center;font-size:.72em;color:#5a6672;">'
                f'{money(v) if show else "&nbsp;"}</td>')
    months = [r[0].split()[0] for r in rows]
    years, prev = [], None
    for r in rows:
        y = r[0].split()[1]
        years.append(y if y != prev else "")
        prev = y
    return (f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;">'
            f'<tr>{plot}</tr><tr>{val}</tr><tr>{_xaxis(months, years)}</tr></table>'
            + (_legend(["month", "trailing 12-month average"], [color, mark],
                        " &nbsp;&mdash;&nbsp; the average needs twelve prior months, so the tick "
                        "begins in Dec 2025.") if average else ""))


def routes(headers, rows):
    """A table whose columns are whatever the caller passes.

    Column count varies by table -- the send edition carries a recipient count the
    others do not -- so this stays generic rather than hard-coding four fields.
    Numeric columns are right-aligned by header name, and a value may be a tuple of
    (text, muted-suffix) to render a grey qualifier after the main text.
    """
    # Right-aligned columns are not all money: a recipient count is an integer that
    # must not pick up a dollar sign.
    MONEY = {"sent", "backed with", "received", "backed by"}
    COUNTS = {"recipients", "backers", "destinations", "candidates"}
    NUMERIC = MONEY | COUNTS
    th = ('text-align:%s;padding:0 12px 7px 0;font-size:.85em;letter-spacing:.06em;'
          'text-transform:uppercase;color:#6b7683;font-weight:600;')
    align = ["right" if h.strip().lower() in NUMERIC else "left" for h in headers]
    out = ['<table style="width:100%;border-collapse:collapse;font-size:.88em;"><tr>']
    for h, al in zip(headers, align):
        out.append(f'<th style="{th % al}">{html.escape(h)}</th>')
    out.append("</tr>")
    for row in rows:
        cells = []
        for i, (val, al) in enumerate(zip(row, align)):
            last = i == len(row) - 1
            pad = "7px 0" if last else "7px 12px 7px 0"
            colour = "color:#444;" if last else ""
            nowrap = "white-space:nowrap;" if al == "right" else ""
            if isinstance(val, tuple):
                text, muted = val
                inner = (colour_party(html.escape(str(text)))
                         + f'<span style="color:#6b7683;"> &middot; '
                           f'{colour_party(html.escape(str(muted)))}</span>'
                         if muted else colour_party(html.escape(str(text))))
            else:
                if isinstance(val, (int, float)):
                    inner = (f"{val:,}" if headers[i].strip().lower() in COUNTS
                             else money(val))
                else:
                    inner = colour_party(html.escape(str(val)))
            cells.append(f'<td style="padding:{pad};border-top:1px solid #ececec;'
                         f'text-align:{al};{nowrap}{colour}">{inner}</td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(out) + "</table>"


# Committee names run past 70 characters ("... Fka Capital One Assoc Political Fund"),
# which doubles every row height across 532 rows. Trimmed for the cell, kept whole in the
# title attribute and in the data-pac search index so nothing becomes unfindable.
def _table_js(px, group=None):
    """Sort/search/filter for one dashboard, parameterised by id prefix.

    `group` names a data attribute whose repeats should be collapsed among the rows that
    are visible right now: the first visible row of each group keeps its `.gl` label and
    the rest swap to a `.gt` tick. Recomputed on every filter and sort, because which row
    comes first changes with both.

    Progressive enhancement only: it reorders and hides rows that the build already
    wrote into the document, so a client that strips scripts still gets the whole
    table. Two dashboards now share it -- the logic is identical and the ids differ.
    """
    return """<script>
(function(){
 var t=document.getElementById('%(p)stab'); if(!t) return;
 var rows=[].slice.call(t.tBodies[0].rows), q=document.getElementById('%(p)sq'),
     n=document.getElementById('%(p)sn'), sel=[].slice.call(document.querySelectorAll('#%(p)sc select')),
     sortK=null, dir=-1, GK=%(g)s;
 function money(v){var a=Math.abs(v),s=a>=1e6?'$'+(a/1e6).toFixed(1)+'M':a>=1e3?'$'+Math.round(a/1e3)+'k':'$'+a;
   return (v<0?'-':'')+s;}
 function apply(){
  var s=(q.value||'').trim().toLowerCase(), f={}, shown=0, sum=0, seen={};
  sel.forEach(function(x){f[x.dataset.f]=x.value;});
  rows.forEach(function(r){
   var d=r.dataset, ok=(!s||(d.q||'').indexOf(s)>=0);
   for(var k in f){ if(f[k] && d[k]!==f[k]) ok=false; }
   r.hidden=!ok;
   if(ok){shown++; if(!seen[d.uniq||shown]){seen[d.uniq||shown]=1; sum+=+d.total;}}
  });
  n.textContent=shown+' of '+rows.length+' shown \u00b7 '+money(sum);
  groups();
 }
 function groups(){
  if(!GK) return;
  var prev=null;
  rows.forEach(function(r){
   if(r.hidden) return;
   var lab=r.querySelector('.gl'), tick=r.querySelector('.gt');
   if(lab){
    var same = prev!==null && r.dataset[GK]===prev;
    lab.style.display = same?'none':'';
    if(tick) tick.style.display = same?'':'none';
   }
   prev=r.dataset[GK];
  });
 }
 function sort(k,num){
  dir = (k===sortK) ? -dir : (num ? -1 : 1);
  sortK=k;
  rows.sort(function(a,b){
   var x=a.dataset[k], y=b.dataset[k];
   if(num){x=+x;y=+y;}
   return x===y ? a.dataset.i-b.dataset.i : (x<y?-1:1)*dir;
  });
  t.setAttribute('data-sorted','1');
  var tb=t.tBodies[0]; rows.forEach(function(r){tb.appendChild(r);});
  groups();
  [].forEach.call(t.tHead.rows[0].cells,function(c){
    c.style.color = c.dataset.k===k ? '#2a78d6' : '#6b7683';});
 }
 [].forEach.call(t.tHead.rows[0].cells,function(c){
   c.onclick=function(){sort(c.dataset.k, c.dataset.num==='1');};});
 q.oninput=apply; sel.forEach(function(x){x.onchange=apply;});
 document.getElementById('%(p)sc').style.display='flex'; apply();
})();
</script>""" % {"p": px, "g": ("'%s'" % group) if group else "null"}


def _clip(text, n):
    """Trim to a width that fits on one line. The full string stays in the title."""
    t = str(text)
    return t if len(t) <= n else t[:n - 1].rstrip(" ,.-") + "\u2026"


def _short(name, n=46):
    name = name.replace(" Political Action Committee", " PAC").replace(" Federal PAC", " PAC")
    return name if len(name) <= n else name[:n - 1].rstrip(" ,.-") + "\u2026"


def recipient_dashboard(channel="Finance & Insurance", period="2026Q2"):
    """Every recipient in the quarter as one table, rendered server-side.

    The rows are static HTML so the table survives an email client with no
    JavaScript; the sort/search layer below only reorders and hides rows that are
    already in the document. That way one copy of the data serves both the emailed
    newsletter and the interactive page, and nothing disappears when scripting is off.
    """
    d = json.loads((REPO / "cache" /
                    f"recipient_table_{slug(channel)}_{slug(period)}.json").read_text())
    rows = d["rows"]
    seated = [r for r in rows if r["seat_in_channel"]]
    top20 = sum(r["total"] for r in rows[:20])

    stats = [("Recipients", f'{d["candidates"]:,}'),
             ("Channel PACs giving", f'{d["senders"]:,}'),
             ("Total", money(d["total"])),
             ("On a finance committee", f'{len(seated)} &middot; {money(sum(r["total"] for r in seated))}'),
             ("Top 20 share", f'{100 * top20 / d["total"]:.0f}%')]
    tiles = "".join(
        f'<td style="padding:10px 14px;border:1px solid #e4e7ea;border-radius:6px;'
        f'background:#fff;vertical-align:top;">'
        f'<div style="font-size:.72em;letter-spacing:.07em;text-transform:uppercase;'
        f'color:#6b7683;font-weight:600;">{html.escape(k)}</div>'
        f'<div style="font-size:1.25em;font-weight:600;color:#1a1a1a;margin-top:2px;">{v}</div></td>'
        f'<td style="width:8px;font-size:0;">&nbsp;</td>' for k, v in stats)

    # Sticky inside the scroll box, so the column you are sorting by stays named while
    # you read row 400. Needs its own opaque background or rows show through it.
    th = ('padding:8px 10px 7px 0;font-size:.78em;letter-spacing:.05em;text-transform:uppercase;'
          'color:#6b7683;font-weight:600;white-space:nowrap;overflow:hidden;'
          'text-overflow:ellipsis;position:sticky;top:0;'
          'background:#fcfcfd;box-shadow:inset 0 -1px 0 #d8dde2;z-index:1;')
    heads = [("Recipient", "left", "name"), ("Seat", "left", "seat"), ("Status", "left", "status"),
             ("Committee seat", "left", "cmte"), ("Received", "right", "total"),
             ("PACs", "right", "pacs"), ("Principal PAC", "left", "pac"),
             ("Primary", "right", "primary"), ("General", "right", "general")]
    numeric = {"total", "pacs", "primary", "general", "cmte"}
    head = "".join(f'<th data-k="{k}" data-num="{int(k in numeric)}" '
                   f'style="text-align:{al};{th}cursor:pointer;">{html.escape(t)}</th>'
                   for t, al, k in heads)

    body = []
    for i, r in enumerate(rows):
        td = ROW
        party = f' ({party_letter(r["party"])})' if r["party"] else ""
        # A net-negative recipient is a refund exceeding the quarter's giving, not an
        # error; it is shown as filed rather than clamped to zero.
        amt = money(r["total"])
        neg = 'color:#b1442a;' if r["total"] < 0 else ""
        # A tick reads at a glance in a 532-row column where a coloured dot does not,
        # and it stays legible in print and under any colour vision.
        mark = ('<span title="sits on a committee this channel covers" '
                'style="color:#1a1a1a;font-weight:700;">&#10003;</span>'
                if r["seat_in_channel"] else
                ('<span title="sits on another mapped committee" '
                 'style="color:#c0392b;">&#10003;</span>' if r["seat_any"] else
                 '<span style="color:#e4e7ea;">&mdash;</span>'))
        body.append(
            f'<tr data-q="{html.escape((r["name"] + " " + r["seat"] + " " + r["top_pac"]).lower())}" '
            f'data-name="{html.escape(r["name"].lower())}" '
            f'data-seat="{html.escape(r["seat"].lower())}" '
            f'data-pac="{html.escape(r["top_pac"].lower())}" '
            f'data-chamber="{r["chamber"]}" data-party="{r["party"][:1] or "?"}" '
            f'data-status="{r["status"]}" data-cmte="{int(r["seat_in_channel"])}" '
            f'data-total="{r["total"]}" data-pacs="{r["pacs"]}" '
            f'data-primary="{r["primary"]}" data-general="{r["general"]}" '
            f'data-i="{i}">'
            f'<td style="{td}">{html.escape(r["name"])}{party}</td>'
            f'<td style="{td}white-space:nowrap;color:#5a6672;">{html.escape(r["seat"])}</td>'
            f'<td style="{td}color:#5a6672;">{html.escape(r["status"])}</td>'
            f'<td style="{td}text-align:center;">{mark}</td>'
            f'<td style="{td}text-align:right;white-space:nowrap;font-weight:600;{neg}">{amt}</td>'
            f'<td style="{td}text-align:right;">{r["pacs"]}</td>'
            f'<td style="{td}color:#444;" title="{html.escape(prettify(r["top_pac"]))}">'
            f'{html.escape(short_cmte(r["top_pac"], 28))}'
            f'<span style="color:#6b7683;"> &middot; {r["top_share"]}%</span></td>'
            f'<td style="{td}text-align:right;white-space:nowrap;color:#5a6672;">'
            f'{money(r["primary"]) if r["primary"] else "&mdash;"}</td>'
            f'<td style="{td}text-align:right;white-space:nowrap;color:#5a6672;">'
            f'{money(r["general"]) if r["general"] else "&mdash;"}</td></tr>')

    controls = (
        '<div id="rc" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px;">'
        '<input id="rq" type="search" placeholder="Search name, seat or PAC"'
        ' style="flex:1 1 220px;min-width:180px;padding:7px 10px;border:1px solid #d8dde2;'
        'border-radius:6px;font:inherit;font-size:.9em;">'
        + "".join(
            f'<select data-f="{f}" style="padding:7px 8px;border:1px solid #d8dde2;'
            f'border-radius:6px;font:inherit;font-size:.9em;background:#fff;">'
            + "".join(f'<option value="{v}">{html.escape(t)}</option>' for v, t in opts)
            + "</select>"
            for f, opts in (
                ("chamber", [("", "Both chambers"), ("House", "House"), ("Senate", "Senate")]),
                ("party", [("", "Any party"), ("D", "Democrat"), ("R", "Republican")]),
                ("status", [("", "Any status"), ("incumbent", "Incumbents"),
                            ("challenger", "Challengers"), ("open seat", "Open seats")]),
                ("cmte", [("", "All recipients"), ("1", "On a finance committee")])))
        + '<span id="rn" style="font-size:.85em;color:#6b7683;white-space:nowrap;"></span></div>')

    js = _table_js("r")


    return (controls
            # A 532-row table is taller than any screen, so it gets its own scroll box
            # rather than pushing the rest of the edition below the fold. Capped at
            # three quarters of the viewport so the surrounding page stays visible and
            # the reader keeps their place in the document.
            + '<div style="max-height:75vh;overflow:auto;border:1px solid #e4e7ea;'
              'border-radius:6px;background:#fff;">'
              '<table id="rtab" style="width:100%;'
              'border-collapse:collapse;font-size:.86em;min-width:720px;">'
            + f"<thead><tr>{head}</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
            + '<p class="legend"><span style="color:#1a1a1a;font-weight:700;">&#10003;</span> sits on a '
              'committee this channel covers &nbsp; <span style="color:#c0392b;font-weight:700;">&#10003;</span> '
              'sits on another mapped committee &nbsp; <span style="color:#e4e7ea;">&mdash;</span> holds no '
              'seat &mdash; click a column to sort.</p>'
            + js), stats, seated, top20


def _unit_short(label):
    """Trim the boilerplate that fronts every committee name, but keep the chamber.

    "House Committee on Financial Services - Capital Markets" is carried by its last two
    segments. Trimming the whole prefix, though, collapsed "House Committee on Armed
    Services" and "Senate Committee on Armed Services" into the same "Armed Services" --
    two different committees under one label, listed one above the other. The prefix being
    removed is exactly what names the chamber, so it goes back on the front, where it
    survives the clip that takes the tail off a long subcommittee name.

    Joint committees and commissions carry no such prefix and are left alone: their own
    names already say what they are.
    """
    for pre in ("House Permanent Select Committee on ", "House Committee on ",
                "Senate Committee on ", "House Select Committee on ",
                "Senate Select Committee on "):
        if label.startswith(pre):
            return f"{pre.split()[0]} {label[len(pre):]}"
    return label


def roster_dashboard(channel="Finance & Insurance", period="2026Q2"):
    """The senior members of every committee this channel covers, and who funds them.

    Ranked by the committee's own seniority order rather than by money, because the
    chair of the subcommittee writing crypto rules matters whether or not she led the
    quarter on dollars. Rank runs separately down each party's list in the source, so
    the top ten is roughly five a side by construction.
    """
    d = json.loads((REPO / "cache" /
                    f"committee_roster_{slug(channel)}_{slug(period)}.json").read_text())
    rows = d["data"]
    # First-appearance order, not alphabetical: the rows arrive in the channel's
    # policy-rank order and the filter should offer committees as the table lists them.
    units, _seen_codes = [], set()
    for r in rows:
        if r["code"] not in _seen_codes:
            _seen_codes.add(r["code"])
            units.append((r["code"], r["unit"]))
    seen, uniq_total = set(), 0
    for r in rows:
        if r["bioguide"] not in seen:
            seen.add(r["bioguide"]); uniq_total += r["total"]
    chairs = [r for r in rows if r["title"]]

    th = ('padding:8px 10px 7px 0;font-size:.78em;letter-spacing:.05em;text-transform:uppercase;'
          'color:#6b7683;font-weight:600;white-space:nowrap;overflow:hidden;'
          'text-overflow:ellipsis;position:sticky;top:0;'
          'background:#fcfcfd;box-shadow:inset 0 -1px 0 #d8dde2;z-index:1;')
    # Declared widths and a fixed layout: without them the browser sizes columns from
    # content and the last one is pushed off the right edge of the scroll box.
    heads = [("Committee", "left", "unit", 0, 17), ("Rank", "right", "rank", 1, 5),
             ("Role", "left", "title", 0, 14), ("Member", "left", "name", 0, 17),
             ("Seat", "left", "seat", 0, 7), ("Collected", "right", "total", 1, 9),
             ("Backers", "right", "backers", 1, 7), ("Principal backer", "left", "pac", 0, 24)]
    head = "".join(f'<th data-k="{k}" data-num="{n}" style="text-align:{al};{th}width:{w}%;'
                   f'cursor:pointer;">{html.escape(t)}</th>' for t, al, k, n, w in heads)

    body = []
    for i, r in enumerate(rows):
        td = ROW
        # Every row carries its committee name, and the script collapses the repeats among
        # the rows currently VISIBLE -- so the name lands on the chair when unfiltered and
        # on whoever is first once a filter or a sort moves things. Baking the repeats in
        # at build time is what went wrong before: a tick continued a committee that
        # sorting had moved elsewhere. With no script at all, every name shows, which is
        # the honest fallback for a client that strips JS.
        unit_cell = (f'<span class="gl" title="{html.escape(r["unit"])}">'
                     f'{html.escape(_short(_unit_short(r["unit"]), 44))}</span>'
                     f'<span class="gt" style="display:none;color:#c8ced4;">&#8942;</span>')
        role = r["title"].replace("Chairwoman", "Chair").replace("Chairman", "Chair")
        role_cell = (f'<span style="color:#1a1a1a;font-weight:600;">{html.escape(role)}</span>'
                     if role else '<span style="color:#c8ced4;">&mdash;</span>')
        party = f' ({party_letter(r["party"])})' if r["party"] else ""
        neg = 'color:#b1442a;' if r["total"] < 0 else ("color:#8b95a1;" if not r["total"] else "")
        pac = (f'{html.escape(short_cmte(r["top"], 26))}'
               f'<span style="color:#6b7683;"> &middot; {r["top_share"]}%</span>'
               if r["top"] else '<span style="color:#c8ced4;">&mdash;</span>')
        body.append(
            f'<tr data-q="{html.escape((r["name"] + " " + r["seat"] + " " + r["unit"] + " " + r["top"]).lower())}" '
            f'data-code="{r["code"]}" data-chamber="{r["chamber"]}" '
            f'data-party="{r["party"] or "?"}" data-role="{"1" if role else ""}" '
            f'data-unit="{html.escape(r["unit"].lower())}" data-rank="{r["rank"]}" '
            f'data-title="{html.escape(role.lower())}" data-name="{html.escape(r["name"].lower())}" '
            f'data-seat="{html.escape(r["seat"].lower())}" data-pac="{html.escape(r["top"].lower())}" '
            f'data-total="{r["total"]}" data-backers="{r["backers"]}" '
            f'data-uniq="{r["bioguide"]}" data-i="{i}">'
            f'<td style="{td}color:#5a6672;">{unit_cell}</td>'
            f'<td style="{td}text-align:right;color:#8b95a1;">{r["rank"]}</td>'
            f'<td style="{td}">{role_cell}</td>'
            f'<td style="{td}white-space:nowrap;">{html.escape(r["name"])}{party}</td>'
            f'<td style="{td}white-space:nowrap;color:#5a6672;">{html.escape(r["seat"])}</td>'
            f'<td style="{td}text-align:right;white-space:nowrap;font-weight:600;{neg}">'
            f'{money(r["total"]) if r["total"] else "&mdash;"}</td>'
            f'<td style="{td}text-align:right;color:#5a6672;">{r["backers"] or "&mdash;"}</td>'
            f'<td style="{td}{SMALL}color:#444;" title="{html.escape(prettify(r["top"]))}">{pac}</td></tr>')

    controls = (
        '<div id="cc" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px;">'
        '<input id="cq" type="search" placeholder="Search member, seat, committee or backer"'
        ' style="flex:1 1 240px;min-width:200px;padding:7px 10px;border:1px solid #d8dde2;'
        'border-radius:6px;font:inherit;font-size:.9em;">'
        '<select data-f="code" style="padding:7px 8px;border:1px solid #d8dde2;border-radius:6px;'
        'font:inherit;font-size:.9em;background:#fff;max-width:260px;">'
        '<option value="">All committees</option>'
        + "".join(f'<option value="{c}">{html.escape(_short(_unit_short(u), 52))}</option>'
                  for c, u in units) + "</select>"
        + "".join(
            f'<select data-f="{f}" style="padding:7px 8px;border:1px solid #d8dde2;'
            f'border-radius:6px;font:inherit;font-size:.9em;background:#fff;">'
            + "".join(f'<option value="{v}">{html.escape(t)}</option>' for v, t in opts)
            + "</select>"
            for f, opts in (
                ("chamber", [("", "Both chambers"), ("House", "House"), ("Senate", "Senate")]),
                ("party", [("", "Any party"), ("D", "Democrat"), ("R", "Republican")]),
                ("role", [("", "All seats"), ("1", "Chairs and ranking members")])))
        + '<span id="cn" style="font-size:.85em;color:#6b7683;white-space:nowrap;"></span></div>')

    table = ('<div style="max-height:75vh;overflow:auto;border:1px solid #e4e7ea;'
             'border-radius:6px;background:#fff;"><table id="ctab" style="width:100%;'
             'border-collapse:collapse;table-layout:fixed;font-size:.86em;min-width:900px;">'
             f"<thead><tr>{head}</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>")
    legend = ('<p class="legend">Committees are listed in order of how central they are to '
              'this channel. Rank is the committee\u2019s own seniority order and runs '
              'separately down each party, so rank 1 is both the chair and the ranking member. '
              'Click a column to sort, and the running total counts each member once however '
              'many seats they hold.</p>')
    return controls + table + legend + _table_js("c", group="code"), d, uniq_total, chairs


def challenger_dashboard(channel="Finance & Insurance", period="2026Q2"):
    """Finance incumbents paired against the money funding the people contesting their seats.

    The other side of every race the channel touches. Attribution runs through a committee
    seat, so the edition names the member holding one; this pairs each of them with the
    candidates contesting that seat. Section B does five such races by hand; this does all
    of them.

    Both columns are ALL support money in the quarter, whoever sent it. Comparing what
    the finance channel gave the incumbent against what anyone gave the challenger would
    not be a comparison, so the channel figure travels separately in the incumbent cell.
    """
    d = json.loads((REPO / "cache" /
                    f"challenger_pairs_{slug(channel)}_{slug(period)}.json").read_text())
    rows = d["rows"]
    beat = [r for r in rows if r["ratio"] and r["ratio"] > 1]

    th = ('padding:8px 10px 7px 0;font-size:.78em;letter-spacing:.05em;text-transform:uppercase;'
          'color:#6b7683;font-weight:600;white-space:nowrap;overflow:hidden;'
          'text-overflow:ellipsis;position:sticky;top:0;'
          'background:#fcfcfd;box-shadow:inset 0 -1px 0 #d8dde2;z-index:1;')
    # Ratio sits beside the two figures it divides. Pushed to the last column it fell off
    # the right edge of the scroll box, which hid the one number the section is about.
    heads = [("Seat", "left", "seat", 0, 7), ("Incumbent", "left", "incumbent", 0, 20),
             ("Backing Incumbent", "right", "incsup", 1, 13),
             ("Challenger", "left", "challenger", 0, 14),
             ("Backing Challenger", "right", "chsup", 1, 13),
             ("Against Challenger", "right", "chopp", 1, 13),
             ("Principal funder", "left", "pac", 0, 20)]
    head = "".join(f'<th data-k="{k}" data-num="{n}" style="text-align:{al};{th}width:{w}%;'
                   f'cursor:pointer;">{html.escape(t)}</th>' for t, al, k, n, w in heads)

    body, last = [], None
    for i, r in enumerate(rows):
        key = (r["seat"], r["incumbent"])
        lead = key != last
        last = key
        # A group starts on a heavier rule so the eye can find where one seat ends.
        td = ("padding:3px 10px 3px 0;white-space:nowrap;border-top:"
              + ("1px solid #d8dde2;" if lead else "1px solid #f4f6f7;"))
        inc_neg = 'color:#b1442a;' if r["inc_support"] < 0 else ""
        # The incumbent's three cells are printed once per seat. Sorting by any column
        # breaks the grouping, so each repeat also carries the full text hidden beside
        # the tick mark, and the sorter swaps which one shows.
        seat_cell = (html.escape(r["seat"]) if lead else
                     f'<span class="gm" style="color:#c8ced4;">&#8942;</span>'
                     f'<span class="gf" style="display:none;">{html.escape(r["seat"])}</span>')
        # No money travels beside the incumbent's name. The group total that used to sit
        # here was challenger money -- an unlabelled figure next to an incumbent reads as
        # the incumbent's, which is the opposite of what it counted. It is per-row in
        # Ch. backed and Against, and the section no longer orders on it.
        inc_txt = f'{html.escape(_clip(r["incumbent"], 19))} ({party_letter(r["inc_party"])})'
        inc_cell = (inc_txt if lead else
                    f'<span class="gm" style="color:#c8ced4;">&#8942;</span>'
                    f'<span class="gf" style="display:none;">{inc_txt}</span>')
        # The full amount only. The channel's own share of it (inc_channel) is still in
        # the data and deliberately not printed: it is a fraction of a fraction, and beside
        # an undivided challenger figure it invited a comparison that is not like for like.
        bac_cell = (money(r["inc_support"]) if lead else
                    f'<span class="gm" style="color:#c8ced4;">&#8942;</span>'
                    f'<span class="gf" style="display:none;">{money(r["inc_support"])}</span>')

        body.append(
            f'<tr data-q="{html.escape((r["seat"] + " " + r["incumbent"] + " " + r["challenger"] + " " + r["ch_top"]).lower())}" '
            f'data-seat="{html.escape(r["seat"].lower())}" data-chamber="{r["chamber"]}" '
            f'data-incparty="{r["inc_party"] or "?"}" data-chparty="{r["ch_party"] or "?"}" '
            f'data-beat="{1 if (r["ratio"] and r["ratio"] > 1) else 0}" '
            f'data-incumbent="{html.escape(r["incumbent"].lower())}" '
            f'data-challenger="{html.escape(r["challenger"].lower())}" '
            f'data-pac="{html.escape(r["ch_top"].lower())}" '
            f'data-incsup="{r["inc_support"]}" data-chsup="{r["ch_support"]}" '
            f'data-chopp="{r["ch_opposed"]}" '
            f'data-total="{r["ch_support"]}" data-i="{i}">'
            f'<td style="{td}white-space:nowrap;color:#5a6672;">{seat_cell}</td>'
            f'<td style="{td}white-space:nowrap;">{inc_cell}</td>'
            f'<td style="{td}text-align:right;white-space:nowrap;{inc_neg}">{bac_cell}</td>'
            f'<td style="{td}white-space:nowrap;">{html.escape(r["challenger"])}'
            f'<span style="color:#6b7683;"> ({r["ch_party"]})</span></td>'
            f'<td style="{td}text-align:right;white-space:nowrap;font-weight:600;">'
            f'{money(r["ch_support"]) if r["ch_support"] else "&mdash;"}</td>'
            f'<td style="{td}text-align:right;white-space:nowrap;color:#5a6672;">'
            f'{money(r["ch_opposed"]) if r["ch_opposed"] else "&mdash;"}</td>'
            f'<td style="{td}color:#444;" title="{html.escape(prettify(r["ch_top"]))}">'
            + (f'{html.escape(short_cmte(r["ch_top"], 22))}'
               f'<span style="color:#6b7683;"> &middot; {r["ch_top_share"]}%</span>'
               if r["ch_top"] else '<span style="color:#c8ced4;">&mdash;</span>')
            + '</td></tr>')

    controls = (
        '<div id="hc" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px;">'
        '<input id="hq" type="search" placeholder="Search seat, incumbent, challenger or funder"'
        ' style="flex:1 1 240px;min-width:200px;padding:7px 10px;border:1px solid #d8dde2;'
        'border-radius:6px;font:inherit;font-size:.9em;">'
        + "".join(
            f'<select data-f="{f}" style="padding:7px 8px;border:1px solid #d8dde2;'
            f'border-radius:6px;font:inherit;font-size:.9em;background:#fff;">'
            + "".join(f'<option value="{v}">{html.escape(t)}</option>' for v, t in opts)
            + "</select>"
            for f, opts in (
                ("chamber", [("", "Both chambers"), ("House", "House"), ("Senate", "Senate")]),
                ("incparty", [("", "Incumbent: any party"), ("D", "Incumbent: Democrat"),
                              ("R", "Incumbent: Republican")]),
                ("beat", [("", "All races"), ("1", "Challenger out-raised the incumbent")])))
        + '<span id="hn" style="font-size:.85em;color:#6b7683;white-space:nowrap;"></span></div>')

    table = ('<style>#htab .gf{display:none}#htab[data-sorted] .gm{display:none}#htab[data-sorted] .gf{display:inline!important}</style>'
             '<div style="max-height:75vh;overflow:auto;border:1px solid #e4e7ea;'
             'border-radius:6px;background:#fff;"><table id="htab" style="width:100%;'
             'border-collapse:collapse;table-layout:fixed;font-size:.84em;min-width:960px;">'
             f"<thead><tr>{head}</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>")
    legend = ('<p class="legend">Challengers are grouped under the incumbent whose seat they '
              'contest; <span style="color:#c8ced4;">&#8942;</span> continues the seat above, and '
              'sorting by any column expands the repeats. Backing Incumbent and Backing '
              'Challenger are all support spending in the period, whoever sent it. '
              '<strong>Against Challenger is money spent opposing the challenger</strong>, not '
              'the incumbent. Click a column to sort; the '
              'running total is challenger support.</p>')
    return controls + table + legend + _table_js("h"), d, beat


def sources_list(urls):
    """The web pages consulted while writing an edition's explanatory sentences.

    These are SELF-REPORTED by the model, not returned by the API: a strict JSON schema
    leaves no free text for the API's url_citation annotations to attach to, so the schema
    asks for the URLs instead. That makes them leads a reader can check rather than proof
    that a claim was checked, and the note says so rather than implying a footnote.
    """
    if not urls:
        return ""
    items = "".join(
        f'<li style="margin:3px 0;"><a href="{html.escape(u)}" '
        f'style="color:#2d5a8e;word-break:break-all;">{html.escape(u)}</a></li>'
        for u in urls)
    return ('<h2>Sources</h2>'
            '<p class="legend">Pages the sentence-writer consulted for committees the FEC '
            'filings alone do not identify, as reported by the model that wrote them. '
            'Every figure in this edition comes from the filings, not from these pages.</p>'
            f'<ol style="font-size:.88em;color:#444;padding-left:22px;">{items}</ol>')


def figure(caption, inner):
    return f'<div class="figure"><p class="figcap">{html.escape(caption)}</p>{inner}</div>'

def note(t):
    return f"<p style='margin-left:18px;color:#444;'>&#8627; {t}</p>"

def doc(body):
    return f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"

# Monthly (label, [values]) in the series order declared at each call site.
# Send keeps the undated bucket separate: it is 73% of the channel's quarter and is
# almost entirely committee-to-committee, so folding it into "primary" -- as this
# chart used to -- reported Fairshake's transfers as primary-season giving.