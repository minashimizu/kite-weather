import requests
import smtplib
import ssl
import os
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# beach_facing: compass direction the beach faces toward the sea (degrees)
SPOTS = [
    {"name": "Miura Beach", "lat": 35.136, "lon": 139.617, "beach_facing": 190},
    {"name": "Futtsu Cape", "lat": 35.302, "lon": 139.822, "beach_facing": 270},
    {"name": "Numazu",      "lat": 35.070, "lon": 138.870, "beach_facing": 185},
    {"name": "Fujikawa",    "lat": 35.150, "lon": 138.670, "beach_facing": 180},
]

WIND_MIN            = 12   # knots
WIND_MAX            = 25   # knots
GUST_SPREAD_MAX     = 12   # knots — gust must not exceed base wind + this value
MIN_CONSECUTIVE_HRS = 3
HOUR_START          = 9    # riding window start (inclusive)
HOUR_END            = 18   # riding window end (exclusive)
DISPLAY_HOUR_START  = 8    # show from 08:00
DISPLAY_HOUR_END    = 20   # show up to 19:00 (exclusive upper bound)

HEAVY_WEATHER_CODES = {55, 63, 65, 73, 75, 82, 95, 96, 99}

WEATHER_CODES = {
    0: "☀️ Clear", 1: "🌤 Mostly clear", 2: "⛅ Partly cloudy", 3: "☁️ Overcast",
    45: "🌫 Fog", 48: "🌫 Icy fog",
    51: "🌦 Light drizzle", 53: "🌧 Drizzle", 55: "🌧 Heavy drizzle",
    61: "🌦 Light rain", 63: "🌧 Rain", 65: "🌧 Heavy rain",
    71: "🌨 Light snow", 73: "❄️ Snow", 75: "❄️ Heavy snow",
    80: "🌦 Showers", 81: "🌧 Rain showers", 82: "⛈ Heavy showers",
    95: "⛈ Thunderstorm", 96: "⛈ Thunderstorm w/ hail", 99: "⛈ Heavy thunderstorm",
}

WIND_DIR_STYLE = {
    "Sideshore":     {"color": "#27ae60", "icon": "✅"},
    "Side-onshore":  {"color": "#27ae60", "icon": "✅"},
    "Onshore":       {"color": "#5d8aa8", "icon": "↓"},
    "Side-offshore": {"color": "#e67e22", "icon": "⚡", "label": "Side-off"},
    "Offshore":      {"color": "#e74c3c", "icon": "🚫"},
}


def wind_speed_color(kn):
    """Return a hex color for a wind speed in knots."""
    if kn is None:
        return "#999"
    if kn < 12:
        return "#999"
    if kn <= 19:
        return "#27ae60"
    if kn <= 28:
        return "#f59e0b"
    return "#ef4444"


def fetch_forecast(lat, lon):
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m,weather_code",
            "wind_speed_unit": "kn",
            "timezone": "Asia/Tokyo",
            "forecast_days": 4,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def kite_wind_dir(wind_deg, beach_facing_deg):
    if wind_deg is None:
        return "—", "#999", ""
    diff = (wind_deg - beach_facing_deg + 360) % 360
    if diff < 22.5 or diff >= 337.5:
        key = "Onshore"
    elif 22.5 <= diff < 67.5 or 292.5 <= diff < 337.5:
        key = "Side-onshore"
    elif 67.5 <= diff < 112.5 or 247.5 <= diff < 292.5:
        key = "Sideshore"
    elif 112.5 <= diff < 157.5 or 202.5 <= diff < 247.5:
        key = "Side-offshore"
    else:
        key = "Offshore"
    style = WIND_DIR_STYLE[key]
    label = style.get("label", key)
    return label, style["color"], style["icon"]


def is_valid_hour(speed, gust, code):
    if speed is None or gust is None:
        return False
    if not (WIND_MIN <= speed <= WIND_MAX):
        return False
    if gust > speed + GUST_SPREAD_MAX:
        return False
    if code in HEAVY_WEATHER_CODES:
        return False
    return True


def has_consecutive_window(valid_times):
    if len(valid_times) < MIN_CONSECUTIVE_HRS:
        return False
    hours = [int(t[11:13]) for t in valid_times]
    run = 1
    for i in range(1, len(hours)):
        run = run + 1 if hours[i] == hours[i - 1] + 1 else 1
        if run >= MIN_CONSECUTIVE_HRS:
            return True
    return False


def find_window_times(valid_times):
    """Return set of times belonging to a run of MIN_CONSECUTIVE_HRS+ consecutive hours."""
    if len(valid_times) < MIN_CONSECUTIVE_HRS:
        return set()
    hours = [int(t[11:13]) for t in valid_times]
    n = len(hours)
    in_window = [False] * n
    run_start = 0
    for i in range(1, n + 1):
        if i == n or hours[i] != hours[i - 1] + 1:
            if i - run_start >= MIN_CONSECUTIVE_HRS:
                for j in range(run_start, i):
                    in_window[j] = True
            run_start = i
    return {valid_times[i] for i in range(n) if in_window[i]}


def get_window_range(valid_times):
    """Return (start_str, end_str, count) of the first qualifying consecutive window.
    end_str is the start of the last slot + 1h (exclusive end), so a 3-slot window
    15:00/16:00/17:00 is displayed as 15:00–18:00 (3h)."""
    if len(valid_times) < MIN_CONSECUTIVE_HRS:
        return None
    hours = [int(t[11:13]) for t in valid_times]
    run_start = 0
    for i in range(1, len(hours) + 1):
        if i == len(hours) or hours[i] != hours[i - 1] + 1:
            count = i - run_start
            if count >= MIN_CONSECUTIVE_HRS:
                start_str = valid_times[run_start][11:16]
                end_hour  = int(valid_times[i - 1][11:13]) + 1
                end_str   = f"{end_hour:02d}:00"
                return start_str, end_str, count
            run_start = i
    return None


def build_html():
    now = datetime.now(JST)
    target_dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 4)]

    # ── Pass 1: fetch & compute all data ─────────────────────────────────────
    all_spot_data = []
    for spot in SPOTS:
        data   = fetch_forecast(spot["lat"], spot["lon"])
        times  = data["hourly"]["time"]
        speeds = data["hourly"]["wind_speed_10m"]
        gusts  = data["hourly"]["wind_gusts_10m"]
        dirs   = data["hourly"]["wind_direction_10m"]
        codes  = data["hourly"]["weather_code"]

        days = []
        spot_has_valid = False

        for date in target_dates:
            # All hours for display window
            display_rows = [
                (t, s, g, d, c)
                for t, s, g, d, c in zip(times, speeds, gusts, dirs, codes)
                if t.startswith(date) and DISPLAY_HOUR_START <= int(t[11:13]) < DISPLAY_HOUR_END
            ]
            # Valid hours (within riding window AND passing all conditions)
            good_times_list = [
                t for t, s, g, d, c in display_rows
                if HOUR_START <= int(t[11:13]) < HOUR_END and is_valid_hour(s, g, c)
            ]
            day_valid    = has_consecutive_window(good_times_list)
            window_times = find_window_times(good_times_list) if day_valid else set()
            window_range = get_window_range(good_times_list) if day_valid else None

            if day_valid:
                spot_has_valid = True

            days.append({
                "date":         date,
                "display_rows": display_rows,
                "good_times":   set(good_times_list),
                "window_times": window_times,
                "window_range": window_range,
                "day_valid":    day_valid,
            })

        all_spot_data.append({
            "spot":      spot,
            "days":      days,
            "has_valid": spot_has_valid,
        })

    # Only keep spots that have at least one valid day
    valid_spots = [sd for sd in all_spot_data if sd["has_valid"]]
    if not valid_spots:
        return None, False

    # ── Summary table ─────────────────────────────────────────────────────────
    day_headers = ""
    for date in target_dates:
        wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
            datetime.strptime(date, "%Y-%m-%d").weekday()
        ]
        day_headers += f'<th style="padding:8px 14px;text-align:center;">{date[5:7]}/{date[8:10]}<br><span style="font-weight:normal;font-size:11px;">{wd}</span></th>'

    summary_rows = ""
    for sd in valid_spots:
        cells = f'<td style="padding:8px 12px;font-weight:bold;">{sd["spot"]["name"]}</td>'
        for day in sd["days"]:
            if day["day_valid"] and day["window_range"]:
                s, e, hrs = day["window_range"]
                cells += f'<td style="padding:8px 12px;text-align:center;background:#f0fdf4;color:#15803d;font-weight:bold;">✅ {s}–{e} ({hrs}h)</td>'
            else:
                cells += '<td style="padding:8px 12px;text-align:center;color:#bdc3c7;">—</td>'
        summary_rows += f"<tr>{cells}</tr>"

    summary_table = f"""
    <table style="border-collapse:collapse;width:100%;margin-bottom:24px;font-size:13px;">
      <thead>
        <tr style="background:#2c3e50;color:white;">
          <th style="padding:8px 12px;text-align:left;">Spot</th>
          {day_headers}
        </tr>
      </thead>
      <tbody>
        {summary_rows}
      </tbody>
    </table>"""

    # ── Detailed blocks ───────────────────────────────────────────────────────
    detail_blocks = []
    for sd in valid_spots:
        spot     = sd["spot"]
        day_html = []

        for day in sd["days"]:
            wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
                datetime.strptime(day["date"], "%Y-%m-%d").weekday()
            ]
            date_label = f"{day['date'][5:7]}/{day['date'][8:10]} ({wd})"

            badge = (
                '<span style="background:#27ae60;color:white;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:bold;">✓ Good to kite</span>'
                if day["day_valid"] else
                '<span style="background:#bdc3c7;color:#555;padding:2px 9px;border-radius:12px;font-size:11px;">No suitable window</span>'
            )

            if not day["day_valid"]:
                # Non-matching day: badge only, no table
                day_html.append(f"""
                <div style="margin-bottom:10px;">
                  <h4 style="margin:8px 0 4px;">{date_label} {badge}</h4>
                </div>""")
                continue

            # Valid day: show all hours 08:00–19:00
            rows_html = ""
            for t, s, g, d, c in day["display_rows"]:
                hour = int(t[11:13])
                in_riding = HOUR_START <= hour < HOUR_END
                in_window = t in day["window_times"]
                is_match  = t in day["good_times"]

                if in_window:
                    bg = "#bbf7d0"          # strong green — part of consecutive window
                elif is_match:
                    bg = "#dbeafe"          # light blue — individually valid
                elif in_riding:
                    bg = "#ffffff"          # riding window, not matching
                else:
                    bg = "#f5f5f5"          # outside riding window

                speed_str   = f"{s:.0f}" if s is not None else "—"
                gust_str    = f"{g:.0f}" if g is not None else "—"
                speed_color = wind_speed_color(s)
                gust_color  = wind_speed_color(g)

                dir_label, dir_color, dir_icon = kite_wind_dir(d, spot["beach_facing"])
                dir_style = f"color:{dir_color};font-weight:bold;" if in_riding else "color:#aaa;"
                dir_cell  = f'<span style="{dir_style}">{dir_icon} {dir_label}</span>'

                weather_label = WEATHER_CODES.get(c, f"Code {c}") if in_riding else f'<span style="color:#bbb;">{WEATHER_CODES.get(c, "")}</span>'

                time_style = "color:#999;" if not in_riding else ""
                rows_html += f"""
                <tr style="background:{bg};">
                  <td style="padding:5px 12px;text-align:center;{time_style}">{t[11:16]}</td>
                  <td style="padding:5px 12px;text-align:center;font-weight:bold;color:{speed_color};">{speed_str}</td>
                  <td style="padding:5px 12px;text-align:center;font-weight:bold;color:{gust_color};">{gust_str}</td>
                  <td style="padding:5px 12px;text-align:center;">{dir_cell}</td>
                  <td style="padding:5px 12px;font-size:12px;">{weather_label}</td>
                </tr>"""

            table = f"""
            <table style="border-collapse:collapse;width:100%;margin:6px 0 16px;font-size:13px;">
              <tr style="background:#2980b9;color:white;font-size:12px;">
                <th style="padding:6px 12px;">Time</th>
                <th style="padding:6px 12px;">Wind (kn)</th>
                <th style="padding:6px 12px;">Gust (kn)</th>
                <th style="padding:6px 12px;">Direction</th>
                <th style="padding:6px 12px;">Weather</th>
              </tr>
              {rows_html}
            </table>"""

            day_html.append(f"""
            <div style="margin-bottom:12px;">
              <h4 style="margin:8px 0 4px;">{date_label} {badge}</h4>
              {table}
            </div>""")

        detail_blocks.append(f"""
        <div style="margin-bottom:28px;">
          <h3 style="color:#2c3e50;border-left:4px solid #2980b9;padding-left:10px;margin-bottom:10px;">
            📍 {spot["name"]}
          </h3>
          {"".join(day_html)}
        </div>""")

    legend = """
    <div style="background:#f8f9fa;border:1px solid #dee2e6;border-radius:6px;padding:10px 14px;margin-bottom:20px;font-size:12px;color:#555;line-height:1.9;">
      <strong>Row highlight:</strong>
      <span style="background:#bbf7d0;padding:1px 7px;border-radius:4px;margin:0 3px;">3h+ window</span>
      <span style="background:#dbeafe;padding:1px 7px;border-radius:4px;margin:0 3px;">Matching hour</span>
      &nbsp;|&nbsp;
      <strong>Direction:</strong>
      ✅ Sideshore &nbsp; ✅ Side-onshore &nbsp; ↓ Onshore &nbsp; ⚡ Side-off &nbsp; 🚫 Offshore
      <br>
      <strong>Wind / Gust color:</strong>
      <span style="color:#999;">■ &lt;12 kn</span> &nbsp;
      <span style="color:#27ae60;">■ 12–19 kn</span> &nbsp;
      <span style="color:#f59e0b;">■ 20–28 kn</span> &nbsp;
      <span style="color:#ef4444;">■ 29+ kn</span>
    </div>"""

    html = f"""
    <html>
    <body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#2c3e50;">
      <div style="background:linear-gradient(135deg,#0077cc,#00b4d8);padding:20px 24px;border-radius:10px;margin-bottom:20px;">
        <h1 style="color:white;margin:0;font-size:22px;">🪁 Kite Surfing Wind Forecast</h1>
        <p style="color:#d6eaf8;margin:6px 0 0;font-size:13px;">
          As of {now.strftime("%Y/%m/%d %H:%M")} JST &nbsp;/&nbsp; {target_dates[0]} – {target_dates[2]}<br>
          Riding window: 09:00–18:00 &nbsp;|&nbsp; Wind: {WIND_MIN}–{WIND_MAX} kn &nbsp;|&nbsp; Max gust: base +{GUST_SPREAD_MAX} kn &nbsp;|&nbsp; Min window: {MIN_CONSECUTIVE_HRS} hrs
        </p>
      </div>
      {summary_table}
      {legend}
      {"".join(detail_blocks)}
      <p style="color:#bdc3c7;font-size:11px;margin-top:30px;border-top:1px solid #eee;padding-top:10px;">
        Data: <a href="https://open-meteo.com" style="color:#bdc3c7;">Open-Meteo</a> &nbsp;/&nbsp; Automated daily at 21:00 JST
      </p>
    </body>
    </html>
    """
    return html, True


def send_email(html_content):
    sender     = os.environ["GMAIL_ADDRESS"]
    password   = os.environ["GMAIL_APP_PASSWORD"]
    recipients = [r.strip() for r in os.environ["RECIPIENTS"].split(",")]

    now = datetime.now(JST)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🪁 Kite Forecast {now.strftime('%m/%d')} — 3-Day Wind Report"
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"✅ Email sent → {', '.join(recipients)}")


if __name__ == "__main__":
    try:
        html, has_conditions = build_html()
        if not has_conditions:
            print("⏭ No qualifying conditions today — email skipped.")
            sys.exit(0)
        send_email(html)
    except KeyError as e:
        print(f"❌ Missing environment variable: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
