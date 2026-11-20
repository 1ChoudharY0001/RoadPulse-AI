
import os
import json
import re
import sqlite3
import difflib
import html

import streamlit as st
import folium
import pandas as pd
import requests
import polyline

from groq import Groq
from supabase import create_client
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation


# ---------------- CONFIGURATION ----------------

st.set_page_config(
    page_title="RoadPulse AI",
    page_icon="🚦",
    layout="wide"
)

# India-wide defaults. The map recentres on actual reports or chosen places.
CITY_NAME = "India"
CITY_LAT = 22.5
CITY_LON = 79.0
INDIA_BOUNDS = {"min_lat": 6.0, "max_lat": 38.0, "min_lon": 68.0, "max_lon": 98.0}

MODEL_NAME = "openai/gpt-oss-20b"


# ---------------- LOAD SECRETS ----------------

def get_secret(name):
    try:
        value = st.secrets.get(name)
        if value:
            return value
    except Exception:
        pass

    return os.environ.get(name)


SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_SECRET_KEY = get_secret("SUPABASE_SECRET_KEY")
GROQ_API_KEY = get_secret("GROQ_API_KEY")
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")


if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    st.error("Supabase configuration is missing.")
    st.stop()

if not GROQ_API_KEY:
    st.error("Groq API key is missing.")
    st.stop()

if not get_secret("ORS_API_KEY"):
    st.warning("OpenRouteService API key is missing. Nationwide autocomplete and route planning will be unavailable until ORS_API_KEY is configured.")


supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SECRET_KEY
)

groq_client = Groq(api_key=GROQ_API_KEY)


# ---------------- AI ANALYSIS ----------------

def analyze_report(location, incident_type, severity, description):

    prompt = f"""
    You are the AI assistant for RoadPulse, a community road
    incident reporting platform in India.

    Analyze ONLY the information provided by the reporter.

    REPORT:
    Location: {location}
    Incident: {incident_type}
    User reported severity: {severity}
    Description: {description}

    STRICT RULES:
    1. Never invent accidents, construction, road closures,
       distances, vehicle counts, or other events.
    2. Do not claim to have checked live traffic or verified
       the incident.
    3. Clearly distinguish reported facts from possible impacts.
    4. Describe traffic impact as an estimate, not a live fact.
    5. Recommendations must be practical and relevant.
    6. If information is insufficient, say so.
    7. Use the user's report as the only source of incident facts.

    Return a valid JSON object with exactly these fields:
    {{
        "category": "incident category",
        "ai_severity": "Low, Medium, or High",
        "summary": "summary based only on reported information",
        "traffic_impact": "potential impact, clearly labelled as an estimate",
        "recommended_action": "practical recommendation",
        "verification_note": "state that the report is community-submitted and unverified",
        "evidence_status": "Community reported - not independently verified"
    }}
    """

    response = groq_client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        response_format={"type": "json_object"},
        temperature=0.2
    )

    result = response.choices[0].message.content

    if not result:
        raise ValueError("AI returned an empty response.")

    return json.loads(result)


# ---------------- AI ROUTE RECOMMENDATION ----------------

def generate_route_recommendation(
    routes,
    route_risk_results,
    recommended_route
):
    route_data = []

    for i, risk in enumerate(route_risk_results):
        route = routes[i]

        incidents = []

        for report in risk.get("nearby_reports", []):
            incidents.append({
                "type": report.get("incident_type", "Unknown"),
                "severity": report.get("severity", "Unknown"),
                "location": report.get("location", "Unknown"),
                "distance_from_route_m": report.get("distance_meters")
            })

        route_data.append({
            "route": f"Route {i + 1}",
            "distance_km": round(
                route["summary"]["distance"] / 1000, 2
            ),
            "estimated_duration_minutes": round(
                route["summary"]["duration"] / 60, 1
            ),
            "safety_score": risk.get("safety_score"),
            "incident_risk": risk.get("risk_level"),
            "risk_score": risk.get("risk_score"),
            "nearby_incident_count": risk.get("nearby_report_count"),
            "blocked_by_reported_closure": risk.get("blocked", False),
            "incidents": incidents
        })

    recommended_index = (
        recommended_route["route_index"] + 1
        if recommended_route is not None else None
    )

    prompt = f"""
You are RoadPulse AI, a traffic route explanation assistant.

Analyze the route data provided below.

ROUTE DATA:
{json.dumps(route_data, indent=2)}

ROUTEPULSE'S CALCULATED RECOMMENDATION:
{recommended_index if recommended_index is not None else "No eligible route"}

STRICT RULES:
1. Use ONLY the supplied route data.
2. Do not invent incidents, traffic conditions, distances or travel times.
3. Do not claim to have checked live traffic.
4. Never recommend a route marked as blocked by a reported road closure.
5. Do not change RoadPulse's calculated recommendation.
6. Explain that incidents are community-submitted reports and may be unverified.
7. A higher safety score means fewer weighted reported hazards, not guaranteed safety.
8. If no eligible route exists, clearly advise the user to verify conditions with traffic authorities.

Write a concise recommendation with:
- Recommended route, or state that no route can be recommended.
- Why it was selected.
- Relevant nearby reported incidents.
- Estimated distance and travel time.
- A short safety disclaimer.

Use clear, helpful language.
"""

    response = groq_client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.2
    )

    result = response.choices[0].message.content

    if not result:
        raise ValueError("AI returned an empty route recommendation.")

    return result.strip()


# ---------------- DATABASE FUNCTIONS ----------------

def get_approved_reports():

    response = (
        supabase.table("traffic_reports")
        .select("*")
        .eq("status", "approved")
        .order("created_at", desc=True)
        .execute()
    )

    return response.data


def get_pending_reports():

    response = (
        supabase.table("traffic_reports")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .execute()
    )

    return response.data



def get_active_approved_reports():
    from datetime import datetime, timedelta, timezone

    response = (
        supabase.table("traffic_reports")
        .select("*")
        .eq("status", "approved")
        .order("created_at", desc=True)
        .execute()
    )

    reports = response.data
    now = datetime.now(timezone.utc)

    expiry_minutes = {
        "accident": 15,
        "traffic congestion": 30,
        "heavy traffic": 30,
        "vehicle breakdown": 15
    }

    persistent_incidents = {
        "road closure",
        "waterlogging",
        "road construction",
        "construction",
        "pothole",
        "road damage"
    }

    def parse_datetime(value):
        if not value:
            return None

        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed

    active_reports = []

    for report in reports:
        try:
            if report.get("resolved_at"):
                continue

            incident = report.get("incident_type", "").strip().lower()

            if incident in expiry_minutes:
                expiry = parse_datetime(report.get("expires_at"))

                if expiry is None:
                    created_at = parse_datetime(report.get("created_at"))

                    if created_at is None:
                        continue

                    expiry = created_at + timedelta(
                        minutes=expiry_minutes[incident]
                    )

                if now >= expiry:
                    continue

                active_reports.append(report)

            elif incident in persistent_incidents:
                active_reports.append(report)

        except (ValueError, TypeError, AttributeError):
            continue

    return active_reports

def get_expired_reports():
    from datetime import datetime, timedelta, timezone

    response = (
        supabase.table("traffic_reports")
        .select("*")
        .eq("status", "approved")
        .order("created_at", desc=True)
        .execute()
    )

    now = datetime.now(timezone.utc)

    temporary_incidents = {
        "accident": 15,
        "traffic congestion": 30,
        "heavy traffic": 30,
        "vehicle breakdown": 15
    }

    expired_reports = []

    for report in response.data:
        incident = report.get("incident_type", "").strip().lower()

        if incident not in temporary_incidents:
            continue

        if report.get("resolved_at"):
            continue

        try:
            expiry = report.get("expires_at")

            if expiry:
                expiry_time = datetime.fromisoformat(
                    expiry.replace("Z", "+00:00")
                )
            else:
                created_at = datetime.fromisoformat(
                    report["created_at"].replace("Z", "+00:00")
                )

                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)

                expiry_time = created_at + timedelta(
                    minutes=temporary_incidents[incident]
                )

            if expiry_time.tzinfo is None:
                expiry_time = expiry_time.replace(tzinfo=timezone.utc)

            if now >= expiry_time:
                report["expiry_time"] = expiry_time.isoformat()
                expired_reports.append(report)

        except (ValueError, TypeError, KeyError):
            continue

    return expired_reports


def get_resolved_reports():

    response = (
        supabase.table("traffic_reports")
        .select("*")
        .eq("status", "resolved")
        .order("resolved_at", desc=True)
        .execute()
    )

    return response.data


def submit_report(report):
    from datetime import datetime, timedelta, timezone

    report = report.copy()
    incident_type = report.get("incident_type", "").strip().lower()

    expiry_minutes = {
        "accident": 15,
        "traffic congestion": 30,
        "heavy traffic": 30,
        "vehicle breakdown": 15
    }

    minutes = expiry_minutes.get(incident_type)

    if minutes is not None:
        report["expires_at"] = (
            datetime.now(timezone.utc) +
            timedelta(minutes=minutes)
        ).isoformat()
    else:
        report["expires_at"] = None

    response = (
        supabase.table("traffic_reports")
        .insert(report)
        .execute()
    )

    return response.data
def update_report_status(report_id, status):

    if status not in ["approved", "rejected"]:
        raise ValueError("Invalid report status.")

    response = (
        supabase.table("traffic_reports")
        .update({"status": status})
        .eq("id", report_id)
        .eq("status", "pending")
        .execute()
    )

    return response.data



def resolve_report(report_id):
    from datetime import datetime, timezone

    response = (
        supabase.table("traffic_reports")
        .update({
            "status": "resolved",
            "resolved_at": datetime.now(timezone.utc).isoformat()
        })
        .eq("id", report_id)
        .eq("status", "approved")
        .execute()
    )

    return response.data


# ---------------- MAP ----------------



# ---------------- TRAFFIC HOTSPOT DETECTION ----------------

def detect_traffic_hotspots(reports, radius_meters=500):

    import math
    from datetime import datetime, timedelta, timezone

    def distance_meters(lat1, lon1, lat2, lon2):

        earth_radius = 6371000

        lat1 = math.radians(float(lat1))
        lon1 = math.radians(float(lon1))
        lat2 = math.radians(float(lat2))
        lon2 = math.radians(float(lon2))

        dlat = lat2 - lat1
        dlon = lon2 - lon1

        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2)
            * math.sin(dlon / 2) ** 2
        )

        return 2 * earth_radius * math.asin(math.sqrt(a))

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    recent_reports = []

    for report in reports:

        try:
            created_at = datetime.fromisoformat(
                report["created_at"].replace("Z", "+00:00")
            )

            if created_at >= cutoff:
                recent_reports.append(report)

        except (KeyError, ValueError, TypeError):
            continue

    count = len(recent_reports)

    visited = set()
    hotspots = []

    for i in range(count):

        if i in visited:
            continue

        cluster = []
        stack = [i]
        visited.add(i)

        while stack:

            current = stack.pop()
            cluster.append(recent_reports[current])

            current_report = recent_reports[current]

            for j in range(count):

                if j in visited:
                    continue

                other_report = recent_reports[j]

                distance = distance_meters(
                    current_report["latitude"],
                    current_report["longitude"],
                    other_report["latitude"],
                    other_report["longitude"]
                )

                if distance <= radius_meters:
                    visited.add(j)
                    stack.append(j)

        if len(cluster) < 2:
            continue

        center_lat = sum(
            float(r["latitude"]) for r in cluster
        ) / len(cluster)

        center_lon = sum(
            float(r["longitude"]) for r in cluster
        ) / len(cluster)

        severity_scores = {
            "Low": 1,
            "Medium": 2,
            "High": 3
        }

        average_score = sum(
            severity_scores.get(r["severity"], 1)
            for r in cluster
        ) / len(cluster)

        if average_score >= 2.5:
            hotspot_severity = "High"
        elif average_score >= 1.5:
            hotspot_severity = "Medium"
        else:
            hotspot_severity = "Low"

        hotspots.append({
            "latitude": center_lat,
            "longitude": center_lon,
            "report_count": len(cluster),
            "severity": hotspot_severity,
            "locations": list(set(
                r["location"] for r in cluster
            )),
            "incident_types": list(set(
                r["incident_type"] for r in cluster
            )),
            "report_ids": [
                r["id"] for r in cluster
            ]
        })

    return hotspots



def create_roadpulse_map(reports, focus=None):
    """Create a nationwide incident map and fit it to the visible reports."""
    valid_points = []
    for report in reports:
        try:
            lat = float(report["latitude"])
            lon = float(report["longitude"])
            if INDIA_BOUNDS["min_lat"] <= lat <= INDIA_BOUNDS["max_lat"] and INDIA_BOUNDS["min_lon"] <= lon <= INDIA_BOUNDS["max_lon"]:
                valid_points.append((lat, lon))
        except (KeyError, TypeError, ValueError):
            continue

    if focus and len(focus) == 2:
        initial_center = [float(focus[0]), float(focus[1])]
        initial_zoom = 12
    elif valid_points:
        initial_center = [
            sum(p[0] for p in valid_points) / len(valid_points),
            sum(p[1] for p in valid_points) / len(valid_points)
        ]
        initial_zoom = 5 if len(valid_points) > 1 else 12
    else:
        initial_center = [CITY_LAT, CITY_LON]
        initial_zoom = 5

    traffic_map = folium.Map(
        location=initial_center,
        zoom_start=initial_zoom,
        tiles="OpenStreetMap",
        control_scale=True
    )

    from folium.plugins import HeatMap
    heatmap_data = []
    severity_weights = {"Low": 1, "Medium": 2, "High": 3}

    for report in reports:
        try:
            lat, lon = float(report["latitude"]), float(report["longitude"])
            if INDIA_BOUNDS["min_lat"] <= lat <= INDIA_BOUNDS["max_lat"] and INDIA_BOUNDS["min_lon"] <= lon <= INDIA_BOUNDS["max_lon"]:
                heatmap_data.append([lat, lon, severity_weights.get(report.get("severity"), 1)])
        except (KeyError, TypeError, ValueError):
            continue

    if heatmap_data:
        heatmap_layer = folium.FeatureGroup(name="Incident Heatmap", show=True)
        HeatMap(heatmap_data, radius=25, blur=18, min_opacity=0.25, max_val=3).add_to(heatmap_layer)
        heatmap_layer.add_to(traffic_map)

    colors = {"High": "red", "Medium": "orange", "Low": "green"}
    for report in reports:
        try:
            lat, lon = float(report["latitude"]), float(report["longitude"])
            if not (INDIA_BOUNDS["min_lat"] <= lat <= INDIA_BOUNDS["max_lat"] and INDIA_BOUNDS["min_lon"] <= lon <= INDIA_BOUNDS["max_lon"]):
                continue
        except (KeyError, TypeError, ValueError):
            continue

        color = colors.get(report.get("severity"), "blue")
        location = html.escape(str(report.get("location", "")))
        incident = html.escape(str(report.get("incident_type", "")))
        severity = html.escape(str(report.get("severity", "")))
        description = html.escape(str(report.get("description", "")))
        popup_html = f"<b>{location}</b><br>Incident: {incident}<br>Severity: {severity}<br>Description: {description}"
        folium.Marker(
            [lat, lon], popup=folium.Popup(popup_html, max_width=300),
            tooltip=incident,
            icon=folium.Icon(color=color, icon="warning-sign")
        ).add_to(traffic_map)

    hotspots = detect_traffic_hotspots(reports)
    for hotspot in hotspots:
        color = {"High": "red", "Medium": "orange", "Low": "green"}.get(hotspot["severity"], "blue")
        popup_html = (
            f"<b>Traffic Hotspot</b><br><b>Severity:</b> {html.escape(str(hotspot['severity']))}"
            f"<br><b>Reports:</b> {hotspot['report_count']}"
            f"<br><b>Locations:</b> {html.escape(', '.join(hotspot['locations']))}"
        )
        folium.CircleMarker(
            [hotspot["latitude"], hotspot["longitude"]], radius=18,
            color=color, fill=True, fill_color=color, fill_opacity=0.35,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"{hotspot['severity']} Hotspot"
        ).add_to(traffic_map)

    if valid_points and not focus:
        traffic_map.fit_bounds(valid_points, padding=(25, 25))
    folium.LayerControl(collapsed=False).add_to(traffic_map)
    return traffic_map


def filter_reports_by_location(reports, query):
    """Filter report records by their saved location text; blank means all India."""
    query = (query or "").strip().casefold()
    if not query:
        return reports
    return [r for r in reports if query in str(r.get("location", "")).casefold()]


# One shared city context controls the user-facing city features.
CITY_CATALOG = {
    "Delhi": (28.6139, 77.2090), "Mumbai": (19.0760, 72.8777),
    "Bengaluru": (12.9716, 77.5946), "Hyderabad": (17.3850, 78.4867),
    "Chennai": (13.0827, 80.2707), "Kolkata": (22.5726, 88.3639),
    "Ahmedabad": (23.0225, 72.5714), "Vadodara": (22.3072, 73.1812),
    "Pune": (18.5204, 73.8567), "Jaipur": (26.9124, 75.7873),
    "Surat": (21.1702, 72.8311), "Indore": (22.7196, 75.8577),
    "Bhopal": (23.2599, 77.4126), "Lucknow": (26.8467, 80.9462),
    "Kanpur": (26.4499, 80.3319), "Nagpur": (21.1458, 79.0882),
    "Nashik": (19.9975, 73.7898), "Rajkot": (22.3039, 70.8022),
    "Gandhinagar": (23.2156, 72.6369), "Coimbatore": (11.0168, 76.9558),
    "Kochi": (9.9312, 76.2673), "Thiruvananthapuram": (8.5241, 76.9366),
    "Visakhapatnam": (17.6868, 83.2185), "Patna": (25.5941, 85.1376),
    "Chandigarh": (30.7333, 76.7794), "Amritsar": (31.6340, 74.8723),
    "Guwahati": (26.1445, 91.7362), "Dehradun": (30.3165, 78.0322)
}



# Extra city coordinates used for typo-tolerant city search
CITY_CATALOG.update({
    "Mysuru": (12.2958, 76.6394),
    "Udaipur": (24.5854, 73.7125),
    "Jodhpur": (26.2389, 73.0243),
    "Agra": (27.1767, 78.0081),
    "Varanasi": (25.3176, 82.9739),
    "Ranchi": (23.3441, 85.3096),
    "Raipur": (21.2514, 81.6296),
    "Thane": (19.2183, 72.9781),
    "Noida": (28.5355, 77.3910),
    "Gurugram": (28.4595, 77.0266),
    "Amritsar": (31.6340, 74.8723),
    "Aurangabad": (19.8762, 75.3433),
    "Mangaluru": (12.9141, 74.8560),
    "Madurai": (9.9252, 78.1198),
    "Guntur": (16.3067, 80.4365),
    "Vijayawada": (16.5062, 80.6480),
    "Jabalpur": (23.1815, 79.9864),
    "Prayagraj": (25.4358, 81.8463)
})


# Expanded offline Indian city catalogue
CITY_CATALOG.update({
    "Srinagar": (34.0837, 74.7973),
    "Jammu": (32.7266, 74.8570),
    "Shimla": (31.1048, 77.1734),
    "Haridwar": (29.9457, 78.1642),
    "Rishikesh": (30.0869, 78.2676),
    "Meerut": (28.9845, 77.7064),
    "Ghaziabad": (28.6692, 77.4538),
    "Faridabad": (28.4089, 77.3178),
    "Mathura": (27.4924, 77.6737),
    "Gwalior": (26.2183, 78.1828),
    "Ujjain": (23.1793, 75.7849),
    "Kolhapur": (16.7050, 74.2433),
    "Solapur": (17.6599, 75.9064),
    "Nanded": (19.1383, 77.3210),
    "Amravati": (20.9374, 77.7796),
    "Bhubaneswar": (20.2961, 85.8245),
    "Cuttack": (20.4625, 85.8830),
    "Puri": (19.8135, 85.8312),
    "Rourkela": (22.2604, 84.8536),
    "Dhanbad": (23.7957, 86.4304),
    "Jamshedpur": (22.8046, 86.2029),
    "Siliguri": (26.7271, 88.3953),
    "Durgapur": (23.5204, 87.3119),
    "Shillong": (25.5788, 91.8933),
    "Imphal": (24.8170, 93.9368),
    "Aizawl": (23.7271, 92.7176),
    "Agartala": (23.8315, 91.2868),
    "Panaji": (15.4909, 73.8278),
    "Tiruchirappalli": (10.7905, 78.7047),
    "Salem": (11.6643, 78.1460),
    "Erode": (11.3410, 77.7172),
    "Vellore": (12.9165, 79.1325),
    "Kozhikode": (11.2588, 75.7804),
    "Thrissur": (10.5276, 76.2144),
    "Kannur": (11.8745, 75.3704),
    "Hubballi": (15.3647, 75.1240),
    "Belagavi": (15.8497, 74.4977),
    "Warangal": (17.9689, 79.5941),
    "Tirupati": (13.6288, 79.4192),
    "Nellore": (14.4426, 79.9865),
    "Rajahmundry": (17.0005, 81.8040),
    "Kakinada": (16.9891, 82.2475),
    "Nizamabad": (18.6725, 78.0941),
    "Karimnagar": (18.4386, 79.1288)
})

def filter_reports_for_city(reports, city, radius_km=45):
    """Filter reports to a city using location text or a configurable centre radius."""
    if not city or city == "All India":
        return reports
    centre = CITY_CATALOG.get(city)
    # The homepage search can select any Indian city returned by ORS, not only
    # the small built-in catalogue. Use that selected place's coordinates too.
    if not centre:
        centre = globals().get("selected_city_center")
    if not centre:
        return filter_reports_by_location(reports, city)
    import math
    city_lat, city_lon = centre
    city_key = str(city).split(",")[0].strip().casefold()
    filtered = []
    for report in reports:
        location_text = str(report.get("location", "")).casefold()
        if city_key in location_text:
            filtered.append(report)
            continue
        try:
            lat, lon = float(report["latitude"]), float(report["longitude"])
            dlat = math.radians(lat - city_lat)
            dlon = math.radians(lon - city_lon)
            a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(city_lat)) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2
            distance = 6371 * 2 * math.asin(min(1, math.sqrt(a)))
            if distance <= radius_km:
                filtered.append(report)
        except (KeyError, TypeError, ValueError):
            continue
    return filtered


@st.cache_data(ttl=3600, show_spinner=False)
def search_offline_india_locations(query):
    """Search Indian populated places using the offline GeoNames database."""
    query = (query or "").strip()

    if len(query) < 2:
        return []

    database = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data",
        "india_locations.db"
    )

    if not os.path.exists(database):
        return []

    conn = None

    try:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro",
            uri=True,
            timeout=10
        )
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. Exact name or ASCII name
        rows = cur.execute("""
            SELECT name, latitude, longitude, admin1, population
            FROM locations
            WHERE name = ? COLLATE NOCASE
               OR ascii_name = ? COLLATE NOCASE
            ORDER BY population DESC
            LIMIT 6
        """, (query, query)).fetchall()

        # 2. Exact alternative name (GeoNames uses comma-separated aliases)
        if not rows:
            rows = cur.execute("""
                SELECT name, latitude, longitude, admin1, population
                FROM locations
                WHERE instr(
                    ',' || LOWER(alternate_names) || ',',
                    ',' || LOWER(?) || ','
                ) > 0
                ORDER BY population DESC
                LIMIT 6
            """, (query,)).fetchall()

        # 3. Prefix match
        if not rows:
            rows = cur.execute("""
                SELECT name, latitude, longitude, admin1, population
                FROM locations
                WHERE name LIKE ? COLLATE NOCASE
                   OR ascii_name LIKE ? COLLATE NOCASE
                ORDER BY population DESC
                LIMIT 6
            """, (query + "%", query + "%")).fetchall()

        # 4. Partial alternative-name match
        if not rows:
            rows = cur.execute("""
                SELECT name, latitude, longitude, admin1, population
                FROM locations
                WHERE alternate_names LIKE ? COLLATE NOCASE
                ORDER BY population DESC
                LIMIT 6
            """, ("%" + query + "%",)).fetchall()

        return [
            {
                "name": row["name"],
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "locality": row["name"],
                "region": row["admin1"] or ""
            }
            for row in rows
        ]

    except (sqlite3.Error, OSError, ValueError):
        return []

    finally:
        if conn is not None:
            conn.close()


@st.cache_data(ttl=900, show_spinner=False)
def get_location_suggestions(query, api_key):
    """Return type-ahead Indian place suggestions from OpenRouteService Pelias."""
    query = (query or "").strip()
    if len(query) < 3 or not api_key:
        return []

    url = "https://api.heigit.org/pelias/v1/autocomplete"
    response = requests.get(
        url,
        params={"api_key": api_key, "text": query, "boundary.country": "IND", "size": 6},
        timeout=20
    )
    response.raise_for_status()
    features = response.json().get("features", [])
    suggestions = []

    for feature in features:
        try:
            lon, lat = feature["geometry"]["coordinates"][:2]
            if not (INDIA_BOUNDS["min_lat"] <= float(lat) <= INDIA_BOUNDS["max_lat"] and INDIA_BOUNDS["min_lon"] <= float(lon) <= INDIA_BOUNDS["max_lon"]):
                continue
            props = feature.get("properties", {})
            label = props.get("label") or props.get("name") or query
            suggestions.append({
                "name": str(label),
                "latitude": float(lat),
                "longitude": float(lon),
                "locality": props.get("locality", ""),
                "region": props.get("region", "")
            })
        except (KeyError, TypeError, ValueError):
            continue
    return suggestions


# ---------------- APPLICATION HEADER ----------------

st.title("🚦 RoadPulse AI")
st.caption(
    "AI-powered community road incident reporting "
    "and traffic intelligence platform"
)

st.info("India-wide coverage: search and report locations across India. Map and analytics reflect available community-submitted reports, not verified live traffic.")

# One city search controls the entire application. Press Enter to apply.
if "roadpulse_selected_city" not in st.session_state:
    st.session_state["roadpulse_selected_city"] = "Delhi"
if "roadpulse_selected_city_center" not in st.session_state:
    st.session_state["roadpulse_selected_city_center"] = CITY_CATALOG["Delhi"]


def _apply_roadpulse_city_search():
    query = st.session_state.get("roadpulse_city_search", "").strip()
    st.session_state.pop("roadpulse_city_search_error", None)
    st.session_state.pop("roadpulse_city_search_match", None)

    if not query:
        return

    if query.casefold() in {"india", "all india"}:
        st.session_state["roadpulse_selected_city"] = "All India"
        st.session_state["roadpulse_selected_city_center"] = (20.5937, 78.9629)
        return

    # First try spelling correction against known Indian cities.
    names = list(CITY_CATALOG.keys())
    closest = difflib.get_close_matches(
        query.casefold(),
        [name.casefold() for name in names],
        n=1,
        cutoff=0.65
    )

    if closest:
        matched_name = next(
            name for name in names if name.casefold() == closest[0]
        )
        st.session_state["roadpulse_selected_city"] = matched_name
        st.session_state["roadpulse_selected_city_center"] = CITY_CATALOG[matched_name]
        st.session_state["roadpulse_city_search_match"] = (
            f"Spelling corrected to {matched_name}"
        )
        return

    # Search the nationwide offline India database first.
    offline_matches = search_offline_india_locations(query)

    if offline_matches:
        match = offline_matches[0]
        st.session_state["roadpulse_selected_city"] = match["name"]
        st.session_state["roadpulse_selected_city_center"] = (
            match["latitude"], match["longitude"]
        )
        return

    # Use ORS only when the offline database has no match.
    try:
        matches = get_location_suggestions(query, get_secret("ORS_API_KEY"))

        if matches:
            match = matches[0]
            st.session_state["roadpulse_selected_city"] = match["name"]
            st.session_state["roadpulse_selected_city_center"] = (
                match["latitude"], match["longitude"]
            )
        else:
            st.session_state["roadpulse_city_search_error"] = (
                f"No Indian location found for: {query}"
            )

    except Exception:
        st.session_state["roadpulse_city_search_error"] = (
            "Location search is temporarily unavailable. "
            "You can still search cities in the built-in city catalogue."
        )

st.text_input(
    "📍 Search city or area across India (press Enter to apply)",
    key="roadpulse_city_search",
    placeholder="Example: Delhi, Mumbai, Mysuru",
    on_change=_apply_roadpulse_city_search,
    help="Search any Indian city or area. Press Enter to update the map and dashboards."
)

selected_city = st.session_state["roadpulse_selected_city"]
selected_city_center = st.session_state["roadpulse_selected_city_center"]

if st.session_state.get("roadpulse_city_search_error"):
    st.warning(st.session_state["roadpulse_city_search_error"])

if selected_city == "All India":
    st.caption("Current view: All India. Features show available community-submitted reports nationwide.")
else:
    st.caption(f"Current city context: {selected_city}. City dashboards use available approved community reports within approximately 45 km of the selected location, plus reports whose location text names the city.")

# ---------------- TABS ----------------

map_tab, report_tab, admin_tab, intelligence_tab, route_tab = st.tabs([
    "🗺️ Traffic Map",
    "📝 Report Incident",
    "🔐 Admin Dashboard",
    "📊 Traffic Intelligence",
    "🧭 Route Navigation"
])




# ---------------- ROUTE NAVIGATION FUNCTIONS ----------------



@st.cache_data(ttl=3600, show_spinner=False)
def geocode_location(place):
    """Resolve a free-text place in India using OpenRouteService Pelias."""
    api_key = get_secret("ORS_API_KEY")
    if not api_key:
        raise ValueError("OpenRouteService API key is missing.")

    response = requests.get(
        "https://api.heigit.org/pelias/v1/search",
        params={"api_key": api_key, "text": place, "boundary.country": "IND", "size": 1},
        timeout=20
    )
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        return None

    feature = features[0]
    lon, lat = feature["geometry"]["coordinates"][:2]
    props = feature.get("properties", {})
    if not (INDIA_BOUNDS["min_lat"] <= float(lat) <= INDIA_BOUNDS["max_lat"] and INDIA_BOUNDS["min_lon"] <= float(lon) <= INDIA_BOUNDS["max_lon"]):
        return None
    return {
        "name": props.get("label") or props.get("name") or place,
        "latitude": float(lat),
        "longitude": float(lon),
        "locality": props.get("locality", ""),
        "region": props.get("region", "")
    }


@st.cache_data(ttl=300)
def calculate_routes(
    start_lat, start_lon, end_lat, end_lon, road_closures=None
):
    """Get alternative driving routes while avoiding reported closures."""

    import math

    api_key = get_secret("ORS_API_KEY")

    if not api_key:
        raise ValueError("OpenRouteService API key is missing.")

    url = "https://api.heigit.org/openrouteservice/v2/directions/driving-car"

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "coordinates": [
            [start_lon, start_lat],
            [end_lon, end_lat]
        ],
        "alternative_routes": {
            "target_count": 3,
            "weight_factor": 1.6,
            "share_factor": 0.5
        },
        "instructions": True
    }

    # Keep only closure reports in a corridor around this journey. This avoids
    # sending unrelated closures from other Indian cities to the directions API.
    lat_margin = max(0.35, abs(end_lat - start_lat) * 0.08)
    lon_margin = max(0.35, abs(end_lon - start_lon) * 0.08)
    min_lat, max_lat = min(start_lat, end_lat) - lat_margin, max(start_lat, end_lat) + lat_margin
    min_lon, max_lon = min(start_lon, end_lon) - lon_margin, max(start_lon, end_lon) + lon_margin

    polygons = []

    for report in (road_closures or []):
        try:
            lat = float(report["latitude"])
            lon = float(report["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
            continue

        radius = 35
        lat_offset = radius / 111320
        lon_offset = radius / (
            111320 * max(abs(math.cos(math.radians(lat))), 0.01)
        )

        ring = []

        for i in range(17):
            angle = 2 * math.pi * i / 16
            ring.append([
                lon + lon_offset * math.cos(angle),
                lat + lat_offset * math.sin(angle)
            ])

        polygons.append([ring])

    if polygons:
        payload["options"] = {
            "avoid_polygons": {
                "type": "MultiPolygon",
                "coordinates": polygons
            }
        }

    response = requests.post(
        url,
        headers=headers,
        params={"geometry_format": "geojson"},
        json=payload,
        timeout=40
    )

    response.raise_for_status()
    data = response.json()

    routes = data.get("routes", [])

    if not routes:
        raise ValueError("No driving route was found.")

    return routes



# ---------------- MAP TAB ----------------

with map_tab:

    st.subheader("Live Community Road Reports")

    try:
        approved_reports = get_active_approved_reports()
        city_reports = filter_reports_for_city(approved_reports, selected_city)
        map_area_query = st.text_input(
            "Optional area / locality filter within the selected city",
            key="nationwide_map_area_filter",
            placeholder="Example: Connaught Place, Bandra West, Whitefield"
        )
        visible_map_reports = filter_reports_by_location(city_reports, map_area_query) if map_area_query.strip() else city_reports

        col1, col2, col3 = st.columns(3)
        col1.metric("Approved Reports (selected city)", len(city_reports))
        col2.metric("Reports shown", len(visible_map_reports))
        col3.metric("Selected city", selected_city)

        roadpulse_map = create_roadpulse_map(visible_map_reports, focus=selected_city_center if selected_city != 'All India' else None)

        st_folium(
            roadpulse_map,
            width=None,
            height=550,
            key=f"roadpulse_map_{selected_city}"
        )

        st.caption(
            "Map markers represent approved community reports. Use the filter to focus on a city or locality; this is not verified live traffic sensor data."
        )

    except Exception as e:
        st.error(f"Could not load traffic map: {e}")


# ---------------- REPORT TAB ----------------

with report_tab:

    st.subheader("Report a Road Incident")

    st.markdown("### 📍 Select the exact incident location")
    st.caption("Click on the map to place a marker at the incident site.")

    incident_search = st.text_input(
        "Search incident location in India",
        key="incident_location_search",
        placeholder="Search a road, area, landmark or address in India"
    )
    incident_suggestions = get_location_suggestions(incident_search, get_secret("ORS_API_KEY")) if len(incident_search.strip()) >= 3 else []
    if incident_suggestions:
        incident_choice = st.selectbox(
            "Choose a suggested incident location",
            options=range(len(incident_suggestions)),
            format_func=lambda i: incident_suggestions[i]["name"],
            key="incident_location_suggestion"
        )
        if st.button("Use selected incident location", key="use_incident_suggestion"):
            chosen = incident_suggestions[incident_choice]
            st.session_state["incident_latitude"] = chosen["latitude"]
            st.session_state["incident_longitude"] = chosen["longitude"]
            st.session_state["incident_location_label"] = chosen["name"]
            st.session_state["incident_map_refresh"] = st.session_state.get("incident_map_refresh", 0) + 1
            st.rerun()

    selected_lat = st.session_state.get("incident_latitude", selected_city_center[0])
    selected_lon = st.session_state.get("incident_longitude", selected_city_center[1])

    location_map = folium.Map(
        location=[selected_lat, selected_lon],
        zoom_start=5 if "incident_latitude" not in st.session_state else 14,
        tiles="OpenStreetMap"
    )

    folium.Marker(
        [selected_lat, selected_lon],
        tooltip="Selected incident location",
        popup="Incident location",
        icon=folium.Icon(color="red", icon="warning-sign")
    ).add_to(location_map)

    map_data = st_folium(
        location_map,
        width=None,
        height=400,
        key="incident_location_picker_" + str(st.session_state.get("incident_map_refresh", 0))
    )

    if map_data and map_data.get("last_clicked"):
        clicked = map_data["last_clicked"]
        st.session_state["incident_latitude"] = float(clicked["lat"])
        st.session_state["incident_longitude"] = float(clicked["lng"])

    latitude = st.session_state.get("incident_latitude", selected_city_center[0])
    longitude = st.session_state.get("incident_longitude", selected_city_center[1])

    st.success(
        f"Selected coordinates: {latitude:.6f}, {longitude:.6f}"
    )

    with st.form("incident_form"):

        location = st.text_input(
            "Location",
            value=st.session_state.get("incident_location_label", selected_city),
            placeholder="Example: MG Road, Bengaluru"
        )

        incident_type = st.selectbox(
            "Incident Type",
            [
                "Accident",
                "Traffic Congestion",
                "Pothole",
                "Road Closure",
                "Waterlogging",
                "Road Construction",
                "Other"
            ]
        )

        severity = st.selectbox(
            "Severity",
            ["Low", "Medium", "High"]
        )

        description = st.text_area(
            "Describe the incident"
        )

        submitted = st.form_submit_button(
            "Submit Road Report",
            use_container_width=True
        )

    if submitted:

        if not location.strip() or not description.strip():
            st.warning("Please enter the location and description.")

        else:
            try:
                with st.spinner("AI is analyzing your report..."):

                    try:
                        ai_analysis = analyze_report(
                            location,
                            incident_type,
                            severity,
                            description
                        )
                    except Exception as ai_error:
                        ai_analysis = None
                        st.warning(
                            f"AI analysis unavailable: {ai_error}. "
                            "Your report can still be submitted."
                        )

                    report = {
                        "location": location.strip(),
                        "incident_type": incident_type,
                        "severity": severity,
                        "description": description.strip(),
                        "latitude": latitude,
                        "longitude": longitude,
                        "ai_analysis": ai_analysis,
                        "status": "pending"
                    }

                    saved_report = submit_report(report)

                st.success(
                    "Your road report has been submitted "
                    "for verification!"
                )

                if ai_analysis:
                    st.subheader("AI Analysis")
                    st.json(ai_analysis)

                st.info(
                    "Your report will appear on the public map "
                    "after admin approval."
                )

            except Exception as e:
                st.error(f"Report submission failed: {e}")


# ---------------- ADMIN TAB ----------------

with admin_tab:

    st.subheader("RoadPulse Admin Dashboard")

    if not ADMIN_PASSWORD:
        st.warning(
            "Admin access is not configured. "
            "Set ADMIN_PASSWORD in Streamlit secrets."
        )

    else:

        entered_password = st.text_input(
            "Admin Password",
            type="password"
        )

        if entered_password and entered_password == ADMIN_PASSWORD:

            st.success("Admin access granted.")

            try:
                pending_reports = get_pending_reports()

                st.metric(
                    "Reports Awaiting Approval",
                    len(pending_reports)
                )

                if not pending_reports:
                    st.info("No pending reports.")

                for report in pending_reports:

                    with st.container(border=True):

                        st.write(
                            f"**{report['location']}**"
                        )

                        st.write(
                            f"Incident: {report['incident_type']}"
                        )

                        st.write(
                            f"Severity: {report['severity']}"
                        )

                        st.write(
                            f"Description: {report['description']}"
                        )

                        if report.get("ai_analysis"):
                            st.write("AI Analysis")
                            st.json(report["ai_analysis"])

                        col1, col2 = st.columns(2)

                        if col1.button(
                            "Approve",
                            key=f"approve_{report['id']}"
                        ):
                            update_report_status(
                                report["id"],
                                "approved"
                            )
                            st.success("Report approved.")
                            st.rerun()

                        if col2.button(
                            "Reject",
                            key=f"reject_{report['id']}"
                        ):
                            update_report_status(
                                report["id"],
                                "rejected"
                            )
                            st.warning("Report rejected.")
                            st.rerun()

                st.divider()
                st.subheader("🚧 Manage Approved Incidents")

                active_reports = get_active_approved_reports()

                st.metric(
                    "Active Approved Incidents",
                    len(active_reports)
                )

                if not active_reports:
                    st.info("No active approved incidents.")

                for report in active_reports:

                    with st.container(border=True):

                        st.write(f"**{report['location']}**")
                        st.write(f"Incident: {report['incident_type']}")
                        st.write(f"Severity: {report['severity']}")
                        st.write(f"Description: {report['description']}")

                        if st.button(
                            "Mark as Resolved",
                            key=f"resolve_{report['id']}"
                        ):
                            result = resolve_report(report["id"])

                            if result:
                                st.success("Incident marked as resolved.")
                            else:
                                st.warning("Incident could not be resolved.")

                            st.rerun()

                st.divider()
                st.subheader("📚 Resolved Incidents History")

                resolved_reports = get_resolved_reports()

                st.metric(
                    "Total Resolved Incidents",
                    len(resolved_reports)
                )

                if resolved_reports:
                    history_rows = []

                    for report in resolved_reports:
                        history_rows.append({
                            "Report ID": report["id"],
                            "Location": report["location"],
                            "Incident": report["incident_type"],
                            "Severity": report["severity"],
                            "Reported At": report.get("created_at"),
                            "Resolved At": report.get("resolved_at")
                        })

                    history_df = pd.DataFrame(history_rows)

                    st.dataframe(
                        history_df,
                        use_container_width=True,
                        hide_index=True
                    )
                else:
                    st.info("No resolved incidents in history yet.")

                st.divider()
                st.subheader("⏳ Expired Incidents History")

                expired_reports = get_expired_reports()

                st.metric(
                    "Total Expired Incidents",
                    len(expired_reports)
                )

                if expired_reports:
                    expired_rows = []

                    for report in expired_reports:
                        expired_rows.append({
                            "Report ID": report["id"],
                            "Location": report["location"],
                            "Incident": report["incident_type"],
                            "Severity": report["severity"],
                            "Reported At": report.get("created_at"),
                            "Expired At": report.get("expiry_time")
                        })

                    expired_df = pd.DataFrame(expired_rows)

                    st.dataframe(
                        expired_df,
                        use_container_width=True,
                        hide_index=True
                    )
                else:
                    st.info("No expired incidents in history yet.")


            except Exception as e:
                st.error(f"Admin dashboard error: {e}")

        elif entered_password:
            st.error("Incorrect admin password.")



# ---------------- TRAFFIC INTELLIGENCE DASHBOARD ----------------

with intelligence_tab:

    st.subheader("RoadPulse Traffic Intelligence")
    st.caption(
        "Insights generated from approved community-submitted reports. "
        "These are not verified live traffic measurements."
    )

    try:
        intelligence_area_query = selected_city
        # All intelligence datasets share the homepage city context.
        reports = filter_reports_for_city(get_active_approved_reports(), selected_city)
        hotspots = detect_traffic_hotspots(reports)
        all_approved_reports = filter_reports_for_city(get_approved_reports(), selected_city)
        total_reports = len(all_approved_reports)

        high_count = sum(
            1 for r in reports if r.get("severity") == "High"
        )
        medium_count = sum(
            1 for r in reports if r.get("severity") == "Medium"
        )
        low_count = sum(
            1 for r in reports if r.get("severity") == "Low"
        )

        resolved_reports = filter_reports_for_city(get_resolved_reports(), selected_city)
        resolved_count = len(resolved_reports)

        total_reviewed = total_reports + resolved_count

        resolution_rate = (
            (resolved_count / total_reviewed) * 100
            if total_reviewed > 0 else 0
        )

        col1, col2, col3, col4, col5 = st.columns(5)

        col1.metric("Approved Reports (Including Expired)", total_reports)
        col2.metric("Resolved Reports", resolved_count)
        col3.metric("High Severity", high_count)
        col4.metric("Detected Hotspots", len(hotspots))
        col5.metric("Resolution Rate", f"{resolution_rate:.1f}%")

        st.divider()

        st.subheader("📊 Approved vs Resolved Incidents")

        comparison_data = pd.Series({
            "Approved (Including Expired)": total_reports,
            "Resolved": resolved_count
        })

        st.bar_chart(comparison_data)

        st.caption(
            "Statistics represent database report statuses, "
            "not independently verified road conditions."
        )

        st.divider()

        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            st.subheader("Incident Types")

            if reports:
                incident_counts = (
                    pd.Series(
                        [r.get("incident_type", "Unknown") for r in reports]
                    )
                    .value_counts()
                )
                st.bar_chart(incident_counts)
            else:
                st.info("No approved incident reports yet.")

        with chart_col2:
            st.subheader("Severity Distribution")

            severity_data = pd.Series({
                "High": high_count,
                "Medium": medium_count,
                "Low": low_count
            })
            st.bar_chart(severity_data)

        st.divider()

        st.subheader("Recent Incident Trend — Last 7 Days")

        if reports:
            trend_df = pd.DataFrame(reports)

            if "created_at" in trend_df.columns:
                trend_df["created_at"] = pd.to_datetime(
                    trend_df["created_at"],
                    utc=True,
                    errors="coerce"
                )

                trend_df = trend_df.dropna(subset=["created_at"])

                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
                trend_df = trend_df[trend_df["created_at"] >= cutoff]

                if not trend_df.empty:
                    trend_df["date"] = (
                        trend_df["created_at"].dt.strftime("%Y-%m-%d")
                    )

                    daily_counts = (
                        trend_df.groupby("date")
                        .size()
                        .sort_index()
                    )

                    st.line_chart(daily_counts)
                else:
                    st.info("No reports recorded in the last 7 days.")
            else:
                st.info("Report timestamps are unavailable.")
        else:
            st.info("No incident data available for trend analysis.")

        st.divider()

        st.subheader("⏰ Peak Traffic Hours Analysis")
        st.caption(
            "Shows the hours when active approved community incidents "
            "were reported most frequently. Times are shown in IST."
        )

        if reports:
            hourly_df = pd.DataFrame(reports)

            if "created_at" in hourly_df.columns:
                hourly_df["created_at"] = (
                    pd.to_datetime(
                        hourly_df["created_at"],
                        utc=True,
                        errors="coerce"
                    ).dt.tz_convert("Asia/Kolkata")
                )

                hourly_df = hourly_df.dropna(subset=["created_at"])

                if not hourly_df.empty:
                    hourly_df["hour"] = (
                        hourly_df["created_at"].dt.hour
                    )

                    hourly_counts = (
                        hourly_df.groupby("hour")
                        .size()
                        .reindex(range(24), fill_value=0)
                    )

                    hourly_counts.index = [
                        f"{hour:02d}:00" for hour in range(24)
                    ]

                    st.bar_chart(hourly_counts)

                    peak_hour = hourly_counts.idxmax()

                    if hourly_counts.max() > 0:
                        st.metric(
                            "Most Active Reporting Hour",
                            peak_hour,
                            f"{int(hourly_counts.max())} reports"
                        )
                else:
                    st.info("No valid incident timestamps available.")
            else:
                st.info("Report timestamps are unavailable.")
        else:
            st.info("No active incident data available.")

        st.divider()

        st.subheader("📅 Day-of-Week Traffic Analysis")
        st.caption(
            "Shows the number of active approved incidents reported "
            "on each day of the week (IST)."
        )

        if reports:
            weekday_df = pd.DataFrame(reports)

            if "created_at" in weekday_df.columns:
                weekday_df["created_at"] = (
                    pd.to_datetime(
                        weekday_df["created_at"],
                        utc=True,
                        errors="coerce"
                    ).dt.tz_convert("Asia/Kolkata")
                )

                weekday_df = weekday_df.dropna(
                    subset=["created_at"]
                )

                if not weekday_df.empty:
                    weekday_order = [
                        "Monday", "Tuesday", "Wednesday",
                        "Thursday", "Friday", "Saturday", "Sunday"
                    ]

                    weekday_df["day"] = (
                        weekday_df["created_at"].dt.day_name()
                    )

                    weekday_counts = (
                        weekday_df["day"]
                        .value_counts()
                        .reindex(weekday_order, fill_value=0)
                    )

                    st.bar_chart(weekday_counts)

                    busiest_day = weekday_counts.idxmax()

                    if weekday_counts.max() > 0:
                        st.metric(
                            "Busiest Reporting Day",
                            busiest_day,
                            f"{int(weekday_counts.max())} reports"
                        )
                else:
                    st.info("No valid incident timestamps available.")
            else:
                st.info("Report timestamps are unavailable.")
        else:
            st.info("No active incident data available.")

        st.divider()

        st.subheader("Detected Traffic Hotspots")

        if hotspots:
            for index, hotspot in enumerate(hotspots, start=1):
                with st.container(border=True):
                    st.write(
                        f"**Hotspot {index} — {hotspot['severity']} Severity**"
                    )
                    st.write(
                        f"Reports: {hotspot['report_count']}"
                    )
                    st.write(
                        f"Locations: {', '.join(hotspot['locations'])}"
                    )
                    st.write(
                        f"Incident types: "
                        f"{', '.join(hotspot['incident_types'])}"
                    )
        else:
            st.info(
                "No hotspots detected. At least two nearby "
                "approved reports within the last 7 days are required."
            )

        # Incident Cluster Analytics
        st.divider()
        st.subheader("📍 Incident Cluster Analytics")

        if hotspots:
            cluster_rows = []

            for index, hotspot in enumerate(
                sorted(
                    hotspots,
                    key=lambda x: x["report_count"],
                    reverse=True
                ),
                start=1
            ):
                cluster_ids = set(hotspot.get("report_ids", []))

                cluster_reports = [
                    r for r in reports
                    if r.get("id") in cluster_ids
                ]

                incident_counts = (
                    pd.Series([
                        r.get("incident_type", "Unknown")
                        for r in cluster_reports
                    ]).value_counts()
                )

                dominant_incident = (
                    incident_counts.index[0]
                    if not incident_counts.empty
                    else "Unknown"
                )

                cluster_rows.append({
                    "Rank": index,
                    "Cluster Severity": hotspot["severity"],
                    "Reports": hotspot["report_count"],
                    "Dominant Incident": dominant_incident,
                    "Locations": ", ".join(hotspot["locations"]),
                    "Latitude": round(hotspot["latitude"], 5),
                    "Longitude": round(hotspot["longitude"], 5)
                })

            cluster_df = pd.DataFrame(cluster_rows)

            st.dataframe(
                cluster_df,
                use_container_width=True,
                hide_index=True
            )

            priority_cluster = cluster_df.iloc[0]

            st.metric(
                "Largest Incident Cluster",
                f"Cluster #{int(priority_cluster['Rank'])}",
                f"{int(priority_cluster['Reports'])} reports"
            )

        else:
            st.info(
                "Cluster analytics will appear when at least "
                "two nearby active incidents are detected."
            )

        # Traffic Congestion Risk Prediction
        st.divider()
        st.subheader("🔮 Traffic Congestion Risk Prediction")
        st.caption(
            "Estimates upcoming risk from approved community reports "
            "recorded during the last 7 days. This is not live traffic data."
        )

        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(ist)
        cutoff_utc = datetime.now(timezone.utc) - timedelta(days=7)

        recent_prediction_reports = []

        for report in reports:
            try:
                created = datetime.fromisoformat(
                    report["created_at"].replace("Z", "+00:00")
                )
                if created >= cutoff_utc:
                    recent_prediction_reports.append(report)
            except (KeyError, ValueError, TypeError):
                continue

        if len(recent_prediction_reports) >= 5:
            hourly_scores = {hour: 0.0 for hour in range(24)}
            severity_weights = {
                "Low": 1,
                "Medium": 2,
                "High": 3
            }

            for report in recent_prediction_reports:
                created = datetime.fromisoformat(
                    report["created_at"].replace("Z", "+00:00")
                ).astimezone(ist)

                hour = created.hour
                weight = severity_weights.get(
                    report.get("severity"), 1
                )
                hourly_scores[hour] += weight

            max_score = max(hourly_scores.values())

            if max_score > 0:
                risk_rows = []

                for offset in range(1, 7):
                    future_hour = (now_ist.hour + offset) % 24
                    score = hourly_scores[future_hour]
                    risk_percent = round(
                        (score / max_score) * 100
                    )

                    if risk_percent >= 70:
                        risk_level = "High"
                    elif risk_percent >= 35:
                        risk_level = "Medium"
                    else:
                        risk_level = "Low"

                    risk_rows.append({
                        "Upcoming Hour": (
                            now_ist + timedelta(hours=offset)
                        ).strftime("%I %p"),
                        "Estimated Risk": risk_level,
                        "Relative Activity Score": risk_percent
                    })

                prediction_df = pd.DataFrame(risk_rows)

                st.dataframe(
                    prediction_df,
                    use_container_width=True,
                    hide_index=True
                )

                st.info(
                    "Risk levels compare historical report severity "
                    "for the same hour of day. They are indicative "
                    "and may be unreliable with limited reports."
                )
            else:
                st.info(
                    "Not enough incident activity to estimate "
                    "upcoming congestion risk."
                )
        else:
            st.info(
                f"At least 5 approved reports from the last 7 days "
                f"are needed. Currently available: "
                f"{len(recent_prediction_reports)}."
            )

        # Smart Traffic Alerts with Automatic Prioritization
        st.divider()
        st.subheader("🚨 Smart Traffic Alerts")

        priority_scores = {
            "Critical": 4,
            "High": 3,
            "Medium": 2,
            "Low": 1
        }

        alert_rows = []

        # Prioritize each active incident exactly once
        for report in reports:
            incident = report.get("incident_type", "Incident").strip().lower()
            severity = report.get("severity", "Low")
            location = report.get("location", "Unknown location")

            if incident == "road closure" or (
                incident == "accident" and severity == "High"
            ):
                priority = "Critical"
            elif incident == "waterlogging" or severity == "High":
                priority = "High"
            elif severity == "Medium" or incident == "vehicle breakdown":
                priority = "Medium"
            else:
                priority = "Low"

            alert_rows.append({
                "Priority": priority,
                "Score": priority_scores[priority],
                "Alert Type": incident.title(),
                "Location": location,
                "Severity": severity,
                "Details": report.get("description", "No description")
            })

        # Add high-priority hotspot alerts
        for index, hotspot in enumerate(hotspots, start=1):
            if hotspot.get("severity") == "High":
                alert_rows.append({
                    "Priority": "High",
                    "Score": priority_scores["High"],
                    "Alert Type": "Traffic Hotspot",
                    "Location": ", ".join(hotspot.get("locations", [])),
                    "Severity": hotspot.get("severity"),
                    "Details": (
                        f"Cluster {index}: "
                        f"{hotspot.get('report_count', 0)} nearby reports"
                    )
                })

        # Add upcoming high-risk hour alerts
        if "prediction_df" in locals():
            high_risk_hours = prediction_df[
                prediction_df["Estimated Risk"] == "High"
            ]

            for _, risk in high_risk_hours.iterrows():
                alert_rows.append({
                    "Priority": "High",
                    "Score": priority_scores["High"],
                    "Alert Type": "Congestion Forecast",
                    "Location": selected_city,
                    "Severity": "High",
                    "Details": (
                        f"Historical reports indicate elevated risk "
                        f"around {risk['Upcoming Hour']}"
                    )
                })

        if alert_rows:
            # Critical alerts first, then High, Medium and Low
            alert_rows.sort(
                key=lambda x: x["Score"],
                reverse=True
            )

            critical_count = sum(
                1 for a in alert_rows if a["Priority"] == "Critical"
            )
            high_count = sum(
                1 for a in alert_rows if a["Priority"] == "High"
            )
            medium_count = sum(
                1 for a in alert_rows if a["Priority"] == "Medium"
            )
            low_count = sum(
                1 for a in alert_rows if a["Priority"] == "Low"
            )

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("🔴 Critical", critical_count)
            col2.metric("🟠 High", high_count)
            col3.metric("🟡 Medium", medium_count)
            col4.metric("🟢 Low", low_count)

            alert_df = pd.DataFrame(alert_rows).drop(columns=["Score"])

            st.dataframe(
                alert_df,
                use_container_width=True,
                hide_index=True
            )
        else:
            st.success(
                "No active traffic incidents or high-priority "
                "alerts detected."
            )

        st.caption(
            "Alerts are prioritized using approved community reports "
            "and historical reporting patterns. They do not confirm "
            "real-time road conditions."
        )

    except Exception as e:
        st.error(f"Traffic Intelligence error: {e}")



# ---------------- ROUTE NAVIGATION TAB ----------------


def calculate_route_incident_risk(routes, reports, corridor_meters=100):
    """Calculate route exposure using active approved community reports."""

    import math
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    severity_weights = {
        "Low": 1.0,
        "Medium": 2.0,
        "High": 3.0
    }

    temporary_incidents = {
        "accident": 15,
        "traffic congestion": 30,
        "heavy traffic": 60,
        "vehicle breakdown": 15
    }

    persistent_incidents = {
        "road closure",
        "waterlogging",
        "road construction",
        "construction",
        "pothole",
        "road damage"
    }

    def parse_datetime(value):
        if not value:
            return None

        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed

    def is_report_active(report):
        try:
            if report.get("resolved_at"):
                return False

            incident = report.get("incident_type", "").strip().lower()

            if incident in temporary_incidents:
                expiry = parse_datetime(report.get("expires_at"))

                # Legacy reports without expires_at:
                # calculate expiry from their original submission time.
                if expiry is None:
                    created_at = parse_datetime(report.get("created_at"))

                    if created_at is None:
                        return False

                    expiry = created_at + timedelta(
                        minutes=temporary_incidents[incident]
                    )

                return now < expiry

            if incident in persistent_incidents:
                return True

            # Unknown incident types are excluded until classified.
            return False

        except (ValueError, TypeError, AttributeError):
            return False

    def distance_to_segment(lat, lon, a, b):
        cos_lat = math.cos(math.radians(lat))

        px = lon * 111320 * cos_lat
        py = lat * 110540

        ax = a[1] * 111320 * cos_lat
        ay = a[0] * 110540
        bx = b[1] * 111320 * cos_lat
        by = b[0] * 110540

        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy

        if length_sq == 0:
            t = 0
        else:
            t = max(0, min(1, (
                (px - ax) * dx + (py - ay) * dy
            ) / length_sq))

        nearest_x = ax + t * dx
        nearest_y = ay + t * dy

        return math.sqrt(
            (px - nearest_x) ** 2 +
            (py - nearest_y) ** 2
        )

    active_reports = [
        report for report in reports
        if is_report_active(report)
    ]

    results = []

    for route_index, route in enumerate(routes):
        geometry = route.get("geometry")

        if isinstance(geometry, str):
            points = polyline.decode(geometry)
        elif isinstance(geometry, dict):
            coordinates = geometry.get("coordinates", [])
            points = [(lat, lon) for lon, lat in coordinates]
        else:
            points = []

        nearby_reports = []
        score = 0.0

        for report in active_reports:
            try:
                lat = float(report["latitude"])
                lon = float(report["longitude"])

                if not points:
                    continue

                if len(points) == 1:
                    distance = math.sqrt(
                        ((lat - points[0][0]) * 110540) ** 2 +
                        ((lon - points[0][1]) * 111320 *
                         math.cos(math.radians(lat))) ** 2
                    )
                else:
                    distance = min(
                        distance_to_segment(
                            lat, lon, points[i], points[i + 1]
                        )
                        for i in range(len(points) - 1)
                    )

                if distance > corridor_meters:
                    continue

                severity = report.get("severity", "Low")
                incident_type = report.get(
                    "incident_type", "Unknown"
                ).strip().lower()

                # Incident-specific hazard multipliers
                hazard_weights = {
                    "road closure": 5.0,
                    "accident": 4.0,
                    "waterlogging": 3.0,
                    "road construction": 2.0,
                    "construction": 2.0,
                    "pothole": 1.5,
                    "road damage": 1.5,
                    "vehicle breakdown": 1.5,
                    "traffic congestion": 1.0,
                    "heavy traffic": 1.0
                }

                severity_score = severity_weights.get(severity, 1.0)
                hazard_multiplier = hazard_weights.get(
                    incident_type, 1.0
                )

                report_score = severity_score * hazard_multiplier
                score += report_score

                nearby_reports.append({
                    "id": report.get("id"),
                    "location": report.get("location", "Unknown"),
                    "incident_type": report.get("incident_type", "Unknown"),
                    "severity": severity,
                    "distance_meters": round(distance),
                    "hazard_multiplier": hazard_multiplier,
                    "score": round(report_score, 2)
                })

            except (KeyError, TypeError, ValueError):
                continue

        if score == 0:
            risk_level = "No nearby reports"
        elif score <= 1.5:
            risk_level = "Low"
        elif score <= 4:
            risk_level = "Moderate"
        else:
            risk_level = "High"

        results.append({
            "route_index": route_index,
            "risk_score": round(score, 2),
            "risk_level": risk_level,
            "safety_score": max(0, round(100 - (score * 5))),
            "nearby_report_count": len(nearby_reports),
            "nearby_reports": nearby_reports
        })

    return results


with route_tab:
    st.subheader("🧭 Plan Your Route")
    st.caption(
        "Find driving routes and alternatives using OpenRouteService road data. "
        "Travel times are estimates, not live traffic predictions."
    )


    st.caption(
        f"Choose your starting point and destination by clicking the map of {selected_city}. "
        "First click selects START; second click selects DESTINATION."
    )

    if "roadpulse_map_start_point" not in st.session_state:
        st.session_state["roadpulse_map_start_point"] = None
    if "roadpulse_map_end_point" not in st.session_state:
        st.session_state["roadpulse_map_end_point"] = None
    if "roadpulse_route_last_click" not in st.session_state:
        st.session_state["roadpulse_route_last_click"] = None

    if st.button("Clear map selections", key="clear_route_map_points"):
        st.session_state["roadpulse_map_start_point"] = None
        st.session_state["roadpulse_map_end_point"] = None
        st.session_state["roadpulse_route_last_click"] = None
        st.rerun()

    route_picker = folium.Map(
        location=list(selected_city_center),
        zoom_start=12 if selected_city != "All India" else 5,
        tiles="OpenStreetMap",
        control_scale=True
    )

    map_start = st.session_state["roadpulse_map_start_point"]
    map_end = st.session_state["roadpulse_map_end_point"]

    if map_start:
        folium.Marker(
            [map_start["latitude"], map_start["longitude"]],
            tooltip="START",
            popup="Selected starting point",
            icon=folium.Icon(color="green", icon="play")
        ).add_to(route_picker)

    if map_end:
        folium.Marker(
            [map_end["latitude"], map_end["longitude"]],
            tooltip="DESTINATION",
            popup="Selected destination",
            icon=folium.Icon(color="red", icon="flag")
        ).add_to(route_picker)

    picker_data = st_folium(
        route_picker,
        width=None,
        height=480,
        key="roadpulse_route_city_picker"
    )

    clicked = picker_data.get("last_clicked") if picker_data else None
    if clicked:
        click_signature = (
            round(float(clicked["lat"]), 6),
            round(float(clicked["lng"]), 6)
        )
        if click_signature != st.session_state["roadpulse_route_last_click"]:
            point = {
                "name": f"Map point ({click_signature[0]:.5f}, {click_signature[1]:.5f})",
                "latitude": click_signature[0],
                "longitude": click_signature[1]
            }
            if not st.session_state["roadpulse_map_start_point"]:
                st.session_state["roadpulse_map_start_point"] = point
                st.session_state["roadpulse_route_last_click"] = click_signature
                st.rerun()
            elif not st.session_state["roadpulse_map_end_point"]:
                st.session_state["roadpulse_map_end_point"] = point
                st.session_state["roadpulse_route_last_click"] = click_signature
                st.rerun()
            else:
                st.info("Both points are selected. Clear selections to choose again.")

    map_start = st.session_state["roadpulse_map_start_point"]
    map_end = st.session_state["roadpulse_map_end_point"]

    st.write("🟢 **Start:**", map_start["name"] if map_start else "Click the map to select")
    st.write("🔴 **Destination:**", map_end["name"] if map_end else "Click the map to select")

    ors_key_for_search = get_secret("ORS_API_KEY")
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Starting location**")

        initial_gps = streamlit_geolocation()

        if (
            initial_gps
            and initial_gps.get("latitude") is not None
            and initial_gps.get("longitude") is not None
        ):
            if st.button(
                "📍 Use My Current GPS Location",
                key="roadpulse_initial_gps"
            ):
                gps_start = {
                    "name": "Your current GPS location",
                    "latitude": float(initial_gps["latitude"]),
                    "longitude": float(initial_gps["longitude"])
                }

                st.session_state["roadpulse_initial_gps_start"] = gps_start
                st.session_state["roadpulse_map_start_point"] = None
                st.rerun()
        else:
            st.info("Allow browser location access to use GPS, or search manually.")

        gps_start_selected = st.session_state.get(
            "roadpulse_initial_gps_start"
        )

        if gps_start_selected:
            st.success(
                f"GPS starting point selected: {gps_start_selected['name']}"
            )
            if st.button(
                "Clear GPS selection",
                key="roadpulse_clear_initial_gps"
            ):
                st.session_state.pop(
                    "roadpulse_initial_gps_start", None
                )
                st.rerun()

        start_place = st.text_input(
            "Search manually (optional)",
            placeholder="Enter starting city, area or landmark",
            key="route_start"
        )
        start_suggestions = get_location_suggestions(start_place, ors_key_for_search) if len(start_place.strip()) >= 3 else []
        if start_suggestions:
            start_choice = st.selectbox(
                "Starting location suggestions",
                options=range(len(start_suggestions)),
                format_func=lambda i: start_suggestions[i]["name"],
                key="route_start_suggestion"
            )
        else:
            start_choice = None

    with col2:
        end_place = st.text_input(
            "Destination",
            placeholder="Enter destination city, area or landmark",
            key="route_end"
        )
        end_suggestions = get_location_suggestions(end_place, ors_key_for_search) if len(end_place.strip()) >= 3 else []
        if end_suggestions:
            end_choice = st.selectbox(
                "Destination suggestions",
                options=range(len(end_suggestions)),
                format_func=lambda i: end_suggestions[i]["name"],
                key="route_end_suggestion"
            )
        else:
            end_choice = None

    if st.button("🔍 Find Routes", key="find_routes"):
        if (
            not st.session_state.get("roadpulse_initial_gps_start")
            and not st.session_state.get("roadpulse_map_start_point")
            and not start_place.strip()
        ) or (
            not st.session_state.get("roadpulse_map_end_point")
            and not end_place.strip()
        ):
            st.warning("Please enter both starting location and destination.")
        elif not ors_key_for_search:
            st.error("OpenRouteService API key is missing. Add ORS_API_KEY to Streamlit secrets.")
        else:
            try:
                with st.spinner("Resolving Indian locations and calculating routes..."):
                    start = (
                        st.session_state.get("roadpulse_initial_gps_start")
                        or st.session_state["roadpulse_map_start_point"]
                        or (start_suggestions[start_choice] if start_choice is not None else geocode_location(start_place.strip()))
                    )
                    end = (
                        st.session_state["roadpulse_map_end_point"]
                        or (end_suggestions[end_choice] if end_choice is not None else geocode_location(end_place.strip()))
                    )

                    if not start:
                        st.error("Starting location could not be found.")
                    elif not end:
                        st.error("Destination could not be found.")
                    else:
                        approved_reports = filter_reports_for_city(get_active_approved_reports(), selected_city)

                        road_closures = [
                            report for report in approved_reports
                            if report.get("incident_type", "").strip().lower()
                            == "road closure"
                        ]

                        routes = calculate_routes(
                            start["latitude"],
                            start["longitude"],
                            end["latitude"],
                            end["longitude"],
                            road_closures=road_closures
                        )

                        st.session_state["roadpulse_routes"] = routes
                        st.session_state["roadpulse_start"] = start
                        st.session_state["roadpulse_end"] = end

                        # Clear previous journey's AI recommendation
                        st.session_state.pop(
                            "roadpulse_ai_recommendation", None
                        )
                        st.session_state.pop(
                            "roadpulse_ai_error", None
                        )

            except Exception as e:
                st.error(f"Could not calculate routes: {e}")

    if "roadpulse_routes" in st.session_state:
        routes = st.session_state["roadpulse_routes"]
        start = st.session_state["roadpulse_start"]
        end = st.session_state["roadpulse_end"]

        st.success("Routes found!")

        st.write("**From:**", start["name"])
        st.write("**To:**", end["name"])

        # DYNAMIC GPS ROUTE RECALCULATION
        st.divider()
        st.subheader("📍 Dynamic Route Recalculation")
        st.caption(
            "If an incident affects your journey, get your current location "
            "and recalculate a route to the same destination."
        )

        gps_location = initial_gps

        if gps_location and gps_location.get("latitude") is not None and gps_location.get("longitude") is not None:
            gps_lat = float(gps_location["latitude"])
            gps_lon = float(gps_location["longitude"])
            gps_accuracy = gps_location.get("accuracy")

            st.success("Current GPS location received.")

            if gps_accuracy is not None:
                st.write(f"GPS accuracy: approximately {gps_accuracy:.0f} metres")

            if st.button("🔄 Recalculate Route from My Location", key="roadpulse_gps_reroute"):
                if gps_accuracy is not None and float(gps_accuracy) > 100:
                    st.warning("GPS accuracy is too low. Please try getting your location again.")
                else:
                    try:
                        current_start = {
                            "name": "Your current GPS location",
                            "latitude": gps_lat,
                            "longitude": gps_lon
                        }

                        with st.spinner("Finding new routes from your current location..."):
                            latest_reports = filter_reports_for_city(
                                get_active_approved_reports(),
                                selected_city
                            )

                            current_closures = [
                                report for report in latest_reports
                                if report.get("incident_type", "").strip().lower()
                                == "road closure"
                            ]

                            new_routes = calculate_routes(
                                gps_lat,
                                gps_lon,
                                end["latitude"],
                                end["longitude"],
                                road_closures=current_closures
                            )

                            if new_routes:
                                st.session_state["roadpulse_routes"] = new_routes
                                st.session_state["roadpulse_start"] = current_start

                                st.session_state.pop(
                                    "roadpulse_ai_recommendation", None
                                )
                                st.session_state.pop(
                                    "roadpulse_ai_error", None
                                )

                                st.success("Your routes have been updated!")
                                st.rerun()
                            else:
                                st.warning("No alternative routes were found.")

                    except Exception as e:
                        st.error(f"Could not recalculate your route: {e}")

        else:
            st.info("Use the location control above to request your current GPS position.")


        # MANUAL STARTING LOCATION SEARCH
        st.markdown("### 🔎 Or Enter Your Current Location")

        manual_place = st.text_input(
            "Search your current location",
            placeholder="Enter city, area, landmark or address",
            key="roadpulse_manual_start"
        )

        manual_suggestions = (
            get_location_suggestions(
                manual_place,
                get_secret("ORS_API_KEY")
            )
            if len(manual_place.strip()) >= 3 else []
        )

        manual_choice = None

        if manual_suggestions:
            manual_choice = st.selectbox(
                "Select your starting location",
                options=range(len(manual_suggestions)),
                format_func=lambda i: manual_suggestions[i]["name"],
                key="roadpulse_manual_start_suggestion"
            )

        if st.button(
            "🔄 Recalculate Route from Searched Location",
            key="roadpulse_manual_reroute"
        ):
            try:
                if manual_choice is not None:
                    manual_start = manual_suggestions[manual_choice]
                else:
                    manual_start = geocode_location(manual_place)

                if not manual_start:
                    st.warning("Location not found. Please enter a more specific location.")
                else:
                    with st.spinner("Finding new routes from your selected location..."):
                        latest_reports = filter_reports_for_city(
                            get_active_approved_reports(),
                            selected_city
                        )

                        current_closures = [
                            report for report in latest_reports
                            if report.get("incident_type", "").strip().lower()
                            == "road closure"
                        ]

                        new_routes = calculate_routes(
                            manual_start["latitude"],
                            manual_start["longitude"],
                            end["latitude"],
                            end["longitude"],
                            road_closures=current_closures
                        )

                        if new_routes:
                            st.session_state["roadpulse_routes"] = new_routes
                            st.session_state["roadpulse_start"] = manual_start

                            st.session_state.pop(
                                "roadpulse_ai_recommendation", None
                            )
                            st.session_state.pop(
                                "roadpulse_ai_error", None
                            )

                            st.success("Routes updated from your searched location!")
                            st.rerun()
                        else:
                            st.warning("No alternative routes were found.")

            except Exception as e:
                st.error(f"Could not recalculate your route: {e}")

        # TRAFFIC INCIDENT RISK COMPARISON
        route_risk_results = []

        try:
            approved_reports = filter_reports_for_city(get_active_approved_reports(), selected_city)

            route_risk_results = calculate_route_incident_risk(
                routes,
                approved_reports
            )

            # BALANCED ROUTE RECOMMENDATION
            risk_values = [
                item["risk_score"] for item in route_risk_results
            ]

            duration_values = [
                route["summary"]["duration"] for route in routes
            ]

            min_risk = min(risk_values)
            max_risk = max(risk_values)
            min_duration = min(duration_values)
            max_duration = max(duration_values)

            for index, item in enumerate(route_risk_results):
                if max_risk > min_risk:
                    normalized_risk = (
                        (item["risk_score"] - min_risk)
                        / (max_risk - min_risk)
                    )
                else:
                    normalized_risk = 0

                if max_duration > min_duration:
                    normalized_duration = (
                        (duration_values[index] - min_duration)
                        / (max_duration - min_duration)
                    )
                else:
                    normalized_duration = 0

                item["balanced_score"] = round(
                    (0.60 * normalized_risk
                     + 0.40 * normalized_duration) * 100,
                    2
                )

            # ROAD CLOSURE AVOIDANCE
            for item in route_risk_results:
                item["blocked"] = any(
                    report.get("incident_type", "").strip().lower()
                    == "road closure"
                    and report.get("distance_meters", 999999) <= 100
                    for report in item["nearby_reports"]
                )

            eligible_routes = [
                item for item in route_risk_results
                if not item["blocked"]
            ]

            recommended_route = (
                min(
                    eligible_routes,
                    key=lambda item: item["balanced_score"]
                )
                if eligible_routes else None
            )

            st.subheader("📊 Traffic Incident Risk Comparison")

            if recommended_route is not None:
                st.success(
                    f"🏆 Recommended route: Route "
                    f"{recommended_route['route_index'] + 1}. "
                    f"{recommended_route['risk_level']} incident risk, "
                    f"{recommended_route['nearby_report_count']} nearby reports. "
                    f"Recommendation score: "
                    f"{recommended_route['balanced_score']}/100."
                )
                st.caption(
                    "Routes with detected road closures within 100 metres "
                    "are excluded from recommendations."
                )
            else:
                st.error(
                    "🚫 No recommended route available. "
                    "All alternative routes have a reported road closure "
                    "within 100 metres."
                )

                st.warning(
                    "Please verify road conditions with traffic authorities "
                    "before travelling. These closures are community reports "
                    "and may require official confirmation."
                )

                closure_details = []

                for risk in route_risk_results:
                    for report in risk["nearby_reports"]:
                        if (
                            report.get("incident_type", "").strip().lower()
                            == "road closure"
                            and report.get("distance_meters", 999999) <= 100
                        ):
                            closure_details.append({
                                "Affected route": (
                                    f"Route {risk['route_index'] + 1}"
                                ),
                                "Closure location": report.get(
                                    "location", "Unknown"
                                ),
                                "Distance from route (m)": (
                                    report["distance_meters"]
                                ),
                                "Severity": report.get(
                                    "severity", "Unknown"
                                )
                            })

                if closure_details:
                    st.subheader("🚧 Reported Closures Affecting Routes")
                    st.dataframe(
                        closure_details,
                        use_container_width=True,
                        hide_index=True
                    )

            risk_table = []

            for i, risk in enumerate(route_risk_results):
                risk_table.append({
                    "Route": f"Route {i + 1}",
                    "Distance (km)": round(
                        routes[i]["summary"]["distance"] / 1000, 2
                    ),
                    "Estimated time (min)": round(
                        routes[i]["summary"]["duration"] / 60, 1
                    ),
                    "Incident risk": risk["risk_level"],
                    "Safety score (/100)": risk["safety_score"],
                    "Nearby active reports": risk["nearby_report_count"],
                    "Risk score": risk["risk_score"],
                    "Recommendation score": risk["balanced_score"],
                    "Route status": (
                        "🚫 BLOCKED — Reported closure"
                        if risk["blocked"] else "Available"
                    ),
                    "Nearest road closure (m)": min(
                        [
                            report["distance_meters"]
                            for report in risk["nearby_reports"]
                            if report.get("incident_type", "").strip().lower()
                            == "road closure"
                        ],
                        default="—"
                    ),
                    "Recommended": (
                        "⭐ Best balance"
                        if recommended_route is not None
                        and risk["route_index"] == recommended_route["route_index"]
                        else ""
                    )
                })

            st.dataframe(
                pd.DataFrame(risk_table),
                use_container_width=True,
                hide_index=True
            )

            # ROADPULSE ROUTE SAFETY EXPLANATIONS
            st.subheader("🔍 Why This Route?")

            for i, risk in enumerate(route_risk_results):
                with st.expander(
                    f"Route {i + 1} — Safety Score: "
                    f"{risk['safety_score']}/100"
                ):
                    reports = risk.get("nearby_reports", [])

                    st.write(
                        f"**Incident risk:** {risk['risk_level']}"
                    )
                    st.write(
                        f"**Nearby reported incidents:** {len(reports)}"
                    )

                    if reports:
                        for report in reports:
                            incident = report.get(
                                "incident_type", "Unknown incident"
                            ).title()
                            severity = report.get("severity", "Unknown")
                            location = report.get("location", "Unknown location")
                            distance = report.get("distance_meters", "Unknown")

                            st.markdown(
                                f"- **{incident}** — {severity} severity; "
                                f"{location}; approximately {distance} m "
                                f"from route."
                            )

                        st.info(
                            "The safety score is reduced according to the "
                            "weighted incident risk reported near this route."
                        )
                    else:
                        st.success(
                            "No active approved community reports were "
                            "detected near this route."
                        )

                    if risk.get("blocked", False):
                        st.error(
                            "This route is excluded from recommendations "
                            "because a reported road closure is nearby."
                        )
                    elif risk["risk_level"] == "High":
                        st.warning(
                            "Consider an alternative route and verify "
                            "conditions before travelling."
                        )
                    else:
                        st.caption(
                            "This assessment is based on community reports, "
                            "not independently verified road conditions."
                        )

            st.caption(
                "Recommendation uses approved community reports near each "
                "route. It is not a live traffic or road-safety guarantee."
            )

            # ROADPULSE AI ROUTE RECOMMENDATION UI
            st.subheader("🤖 AI Route Recommendation")

            if st.button(
                "✨ Generate AI Route Recommendation",
                key="generate_roadpulse_ai_recommendation"
            ):
                st.session_state["roadpulse_map_refresh"] = (
                    st.session_state.get("roadpulse_map_refresh", 0) + 1
                )

                try:
                    with st.spinner(
                        "RoadPulse AI is comparing the available routes..."
                    ):
                        st.session_state["roadpulse_ai_recommendation"] = (
                            generate_route_recommendation(
                                routes,
                                route_risk_results,
                                recommended_route
                            )
                        )
                    st.session_state["roadpulse_ai_error"] = None

                except Exception as ai_error:
                    st.session_state["roadpulse_ai_error"] = str(ai_error)
                    st.session_state.pop(
                        "roadpulse_ai_recommendation", None
                    )

            if st.session_state.get("roadpulse_ai_error"):
                st.error(
                    "AI route recommendation failed: "
                    + st.session_state["roadpulse_ai_error"]
                )

            if st.session_state.get("roadpulse_ai_recommendation"):
                st.markdown("### 🧠 AI Analysis")
                st.info(
                    st.session_state["roadpulse_ai_recommendation"]
                )


        except Exception as e:
            st.warning(
                f"Incident risk comparison is currently unavailable: {e}"
            )

        route_options = [
            f"Route {i+1} — {route['summary']['distance']/1000:.2f} km — "
            f"{route['summary']['duration']/60:.1f} min"
            for i, route in enumerate(routes)
        ]

        selected_route = st.selectbox(
            "Choose a route",
            options=range(len(routes)),
            format_func=lambda i: route_options[i],
            key="selected_roadpulse_route"
        )

        route = routes[selected_route]

        # SHOW ACTIVE INCIDENTS NEAR THE SELECTED ROUTE
        if route_risk_results:
            selected_risk = route_risk_results[selected_route]

            st.subheader("🚨 Turn-by-Turn Incident Warnings")

            nearby_incidents = selected_risk["nearby_reports"]

            if nearby_incidents:
                for incident in sorted(
                    nearby_incidents,
                    key=lambda x: x.get("distance_meters", 999999)
                ):
                    severity = incident.get("severity", "Low")
                    distance = incident.get("distance_meters", 0)
                    incident_name = incident.get("incident_type", "Incident").title()
                    location = incident.get("location", "Unknown location")

                    if severity == "High" and distance <= 100:
                        st.error(
                            f"🔴 CRITICAL: {incident_name} near {location} "
                            f"— approximately {distance} m from your route."
                        )
                    elif severity == "Medium" and distance <= 100:
                        st.warning(
                            f"🟠 WARNING: {incident_name} near {location} "
                            f"— approximately {distance} m from your route."
                        )
                    else:
                        st.info(
                            f"🟡 CAUTION: {incident_name} near {location} "
                            f"— approximately {distance} m from your route."
                        )

                st.subheader("🚧 Active Reports Near Selected Route")

                incident_table = pd.DataFrame(
                    nearby_incidents
                )

                st.dataframe(
                    incident_table[
                        [
                            "location",
                            "incident_type",
                            "severity",
                            "distance_meters"
                        ]
                    ].rename(columns={
                        "location": "Location",
                        "incident_type": "Incident",
                        "severity": "Severity",
                        "distance_meters": "Distance from route (m)"
                    }),
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.success("No active approved reports found near this route.")

        metric1, metric2 = st.columns(2)
        metric1.metric("Distance", f"{route['summary']['distance']/1000:.2f} km")
        metric2.metric("Estimated time", f"{route['summary']['duration']/60:.1f} min")

        route_map = folium.Map(
            location=[start["latitude"], start["longitude"]],
            zoom_start=13,
            tiles="OpenStreetMap"
        )

        folium.Marker(
            [start["latitude"], start["longitude"]],
            tooltip="Starting location",
            popup="Start",
            icon=folium.Icon(color="green", icon="play")
        ).add_to(route_map)

        folium.Marker(
            [end["latitude"], end["longitude"]],
            tooltip="Destination",
            popup="Destination",
            icon=folium.Icon(color="red", icon="flag")
        ).add_to(route_map)

        # ROADPULSE RECOMMENDED ROUTE HIGHLIGHT
        map_recommended_route = None

        if route_risk_results:
            map_eligible_routes = [
                item for item in route_risk_results
                if not item.get("blocked", False)
            ]

            if map_eligible_routes:
                map_recommended_route = min(
                    map_eligible_routes,
                    key=lambda item: item["balanced_score"]
                )["route_index"]

        # Draw all alternatives; highlight the selected route.
        for i, alternative in enumerate(routes):
            geometry = alternative.get("geometry")

            if isinstance(geometry, str):
                # Decode OpenRouteService encoded polyline
                points = polyline.decode(geometry)

            elif isinstance(geometry, dict):
                # Support GeoJSON geometry too
                coordinates = geometry.get("coordinates", [])
                points = [(lat, lon) for lon, lat in coordinates]

            else:
                points = []

            folium.PolyLine(
                points,
                color=(
                    "green" if i == map_recommended_route
                    else "blue" if i == selected_route
                    else "gray"
                ),
                weight=7 if i == selected_route else 4,
                opacity=0.9 if i == selected_route else 0.5,
                tooltip=f"Route {i+1}"
            ).add_to(route_map)

        # ROADPULSE ROAD CLOSURE MAP MARKERS: limit display to this journey area.
        active_map_reports = filter_reports_for_city(get_active_approved_reports(), selected_city)
        route_lat_margin = max(0.35, abs(end["latitude"] - start["latitude"]) * 0.08)
        route_lon_margin = max(0.35, abs(end["longitude"] - start["longitude"]) * 0.08)
        route_min_lat = min(start["latitude"], end["latitude"]) - route_lat_margin
        route_max_lat = max(start["latitude"], end["latitude"]) + route_lat_margin
        route_min_lon = min(start["longitude"], end["longitude"]) - route_lon_margin
        route_max_lon = max(start["longitude"], end["longitude"]) + route_lon_margin

        for closure in active_map_reports:
            if closure.get("incident_type", "").strip().lower() != "road closure":
                continue

            try:
                closure_lat = float(closure["latitude"])
                closure_lon = float(closure["longitude"])
            except (KeyError, TypeError, ValueError):
                continue

            if not (route_min_lat <= closure_lat <= route_max_lat and route_min_lon <= closure_lon <= route_max_lon):
                continue

            closure_location = closure.get("location", "Unknown location")
            closure_severity = closure.get("severity", "Unknown")

            folium.Circle(
                location=[closure_lat, closure_lon],
                radius=35,
                color="red",
                fill=True,
                fill_color="red",
                fill_opacity=0.15,
                tooltip="Approximate road-closure avoidance area"
            ).add_to(route_map)

            folium.Marker(
                location=[closure_lat, closure_lon],
                tooltip=f"🚧 Road Closure — {closure_location}",
                popup=(
                    f"<b>🚧 Reported Road Closure</b><br>"
                    f"Location: {closure_location}<br>"
                    f"Severity: {closure_severity}<br>"
                    f"Status: Community report"
                ),
                icon=folium.Icon(
                    color="red",
                    icon="warning-sign",
                    prefix="glyphicon"
                )
            ).add_to(route_map)

        route_map.fit_bounds([
            [start["latitude"], start["longitude"]],
            [end["latitude"], end["longitude"]]
        ])

        st.subheader("🗺️ Route Map")
        st_folium(
            route_map,
            width=None,
            height=500,
            key=(
                "roadpulse_route_map_"
                + str(st.session_state.get("roadpulse_map_refresh", 0))
            )
        )

        st.subheader("📍 Turn-by-turn Directions")

        steps = route["segments"][0]["steps"]

        for i, step in enumerate(steps, 1):
            instruction = step.get("instruction", "Continue")
            road_name = step.get("name") or "Unnamed road"
            distance = step.get("distance", 0)

            st.write(
                f"**{i}. {instruction}** — {road_name} "
                f"({distance:.0f} m)"
            )

        st.caption(
            "Directions and route times are based on the road network. "
            "They do not guarantee a traffic-free journey."
        )

