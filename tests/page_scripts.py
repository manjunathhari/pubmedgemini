"""The only scripts any page may carry: the SurgeryAi style kit's day/night switch (owner, 13 Nov:
"Also put the day and night toggle"), the one exception to the no-JavaScript rule. Both live in
base.html, inline, word for word as pinned here, so nothing else can slip in. Tests that forbid
scripts check the page with these two taken out (`without_theme_scripts`)."""

import re

# in <head>: applies a saved choice before the first paint (the kit's key, wrapped in try/catch)
HEAD_SCRIPT = ('<script>try{var t=localStorage.getItem("surgeryai-theme");'
               'if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)'
               '}catch(e){}</script>')
# at the end of <body>: the switch sets day or night outright and remembers it on this device
SWITCH_SCRIPT = ('<script>document.querySelector(".sai-daynight").addEventListener("click",'
                 'function(){var r=document.documentElement,s=r.getAttribute("data-theme"),'
                 'n=s?s==="dark":matchMedia("(prefers-color-scheme: dark)").matches,'
                 'x=n?"light":"dark";r.setAttribute("data-theme",x);'
                 'try{localStorage.setItem("surgeryai-theme",x)}catch(e){}});</script>')

# the header's switch, as base.html writes it (the moon and sun are CSS masks, not inline <svg>)
SWITCH_BUTTON = ('<button type="button" class="sai-daynight" aria-label="Switch between day and '
                 'night"><span data-for="day" aria-hidden="true"></span><span data-for="night" '
                 'aria-hidden="true"></span></button>')

# the links in <head>: the stylesheet, then the SAi badge favicons (day, night) and the home-screen icon
HEAD_LINKS = [
    '<link rel="stylesheet" href="/static/app.css">',
    ('<link rel="icon" type="image/svg+xml" href="/static/icons/sai-badge-day.svg" '
     'media="(prefers-color-scheme: light)">'),
    ('<link rel="icon" type="image/svg+xml" href="/static/icons/sai-badge-night.svg" '
     'media="(prefers-color-scheme: dark)">'),
    '<link rel="apple-touch-icon" href="/static/icons/sai-badge-day-180.png">',
]
LINK_HREFS = [re.search(r'href="([^"]+)"', link).group(1) for link in HEAD_LINKS]


def without_theme_scripts(html: str) -> str:
    """`html` with the two allowed scripts removed; fails unless each is there exactly once, the
    head one inside <head> and the switch one just before </body>."""
    assert html.count(HEAD_SCRIPT) == 1, "the head script is missing or doubled"
    assert html.count(SWITCH_SCRIPT) == 1, "the switch script is missing or doubled"
    assert html.index(HEAD_SCRIPT) < html.index("</head>")
    assert html.rstrip().endswith(SWITCH_SCRIPT + "\n</body>\n</html>")
    return html.replace(HEAD_SCRIPT, "").replace(SWITCH_SCRIPT, "")
