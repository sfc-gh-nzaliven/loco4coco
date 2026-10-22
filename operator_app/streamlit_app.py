"""Loco for CoCo - Solution Engineer operator console.

Shows booth visitors as they finish (LOCO4COCO.BOOTH.SESSIONS) and lets the
operator correct the SE scores and the visitor's inputs. Edits recompute the
average and tier with the same rule as game/server.py, and are stamped into
SE_SCORING.manual so a corrected row is never mistaken for a model score.
"""
import json
import os

import pandas as pd
import streamlit as st

TABLE = "LOCO4COCO.BOOTH.SESSIONS"
PASS_DEFAULT, BLUEPRINT_DEFAULT = 3.0, 2.0
TIER_LABEL = {"qualified": "Qualified for SE", "blueprint": "Blueprint only",
              "rejected": "Rejected", None: "Unscored"}
TIER_ICON = {"qualified": ":material/emoji_events:", "blueprint": ":material/description:",
             "rejected": ":material/block:", None: ":material/help:"}

st.set_page_config(page_title="Loco for CoCo - SE console", layout="wide",
                   page_icon=":material/support_agent:")


@st.cache_resource
def session():
    conn = st.connection(
        "snowflake",
        connection_name=os.getenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME") or "default")
    return conn.session()


def query(sql, params=None):
    return session().sql(sql, params=params or []).to_pandas()


def run(sql, params):
    session().sql(sql, params=params).collect()


def tier_for(total, pass_t, blue_t):
    # Exact-sum comparison, as in server.py, so 7/3 is never rounded into a pass.
    if total >= 3 * pass_t - 1e-9:
        return "qualified"
    if total >= 3 * blue_t - 1e-9:
        return "blueprint"
    return "rejected"


def as_list(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [v]
    return list(v) if isinstance(v, (list, tuple)) else [v]


def as_obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v) or {}
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def text(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


FEED_SQL = f"""
select session_id, session_ts, first_name, company, industry, poc_name,
       problem_statement, score, se_score_now, se_score_solvable,
       se_score_consumption, se_score_avg, se_qualified,
       se_scoring:tier::string as tier,
       coalesce(se_scoring:manual:edited::boolean, false) as edited,
       delivery_status
from {TABLE}
where session_ts >= dateadd(hour, ?, current_timestamp())
order by session_ts desc
limit 300
"""

st.title("Loco for CoCo - Solution Engineer console")

with st.sidebar:
    hours = st.select_slider("Show visitors from the last",
                             options=[1, 3, 8, 24, 72, 168], value=24,
                             format_func=lambda h: f"{h} h" if h < 24 else f"{h // 24} d")
    tiers = st.multiselect("Tier", ["qualified", "blueprint", "rejected"],
                           default=["qualified", "blueprint", "rejected"],
                           format_func=lambda t: TIER_LABEL[t])
    live = st.toggle("Live refresh (10 s)", value=True)
    operator = st.text_input("Your name (saved as SE_OPERATOR)",
                             value=st.session_state.get("operator", ""))
    st.session_state["operator"] = operator


@st.fragment(run_every="10s" if live else None)
def feed():
    try:
        df = query(FEED_SQL, [-hours])
    except Exception as e:  # noqa: BLE001 - show the reason, keep the page up
        st.error(f"Could not read {TABLE}: {e}")
        return
    df.columns = [c.lower() for c in df.columns]
    df = df[df["tier"].isin(tiers) | (df["tier"].isna() & (len(tiers) == 3))]

    q = (df["tier"] == "qualified").sum()
    with st.container(horizontal=True):
        st.metric("Visitors", len(df), border=True)
        st.metric("Qualified for SE", int(q), border=True)
        st.metric("Blueprint only", int((df["tier"] == "blueprint").sum()), border=True)
        st.metric("Rejected", int((df["tier"] == "rejected").sum()), border=True)
        st.metric("Edited by an SE", int(df["edited"].sum()), border=True)

    if df.empty:
        st.info("No visitors in this window yet.")
        return

    view = pd.DataFrame({
        "When": pd.to_datetime(df["session_ts"]),
        "Tier": df["tier"].map(lambda t: TIER_LABEL.get(t, "Unscored")),
        "Name": df["first_name"],
        "Company": df["company"],
        "Use case": df["poc_name"],
        "Now": df["se_score_now"],
        "Solvable": df["se_score_solvable"],
        "Consumption": df["se_score_consumption"],
        "Avg": pd.to_numeric(df["se_score_avg"], errors="coerce"),
        "Game score": df["score"],
        "Edited": df["edited"],
    })
    pick = st.dataframe(
        view, hide_index=True, on_select="rerun", selection_mode="single-row",
        key="feed_table",
        column_config={
            "When": st.column_config.DatetimeColumn(format="HH:mm  D MMM"),
            "Avg": st.column_config.ProgressColumn(min_value=1, max_value=5, format="%.1f"),
            "Edited": st.column_config.CheckboxColumn(),
        })
    rows = pick.selection.rows if pick and pick.selection else []
    if rows:
        new_id = df.iloc[rows[0]]["session_id"]
        if st.session_state.get("selected") != new_id:
            st.session_state["selected"] = new_id
            st.rerun()


feed()

sid = st.session_state.get("selected")
if not sid:
    st.caption("Select a visitor in the table to read their brief and edit the scores.")
    st.stop()

d = query(f"select * from {TABLE} where session_id = ?", [sid])
if d.empty:
    st.warning("That visitor is no longer in the table.")
    st.stop()
r = {k.lower(): v for k, v in d.iloc[0].to_dict().items()}
scoring = as_obj(r.get("se_scoring"))
pass_t = float(scoring.get("threshold") or PASS_DEFAULT)
blue_t = float(scoring.get("blueprint_threshold") or BLUEPRINT_DEFAULT)
tier = scoring.get("tier")

st.divider()
st.subheader(f"{text(r.get('first_name'))} - {text(r.get('company'))}")
st.badge(TIER_LABEL.get(tier, "Unscored"), icon=TIER_ICON.get(tier),
         color={"qualified": "green", "blueprint": "orange",
                "rejected": "red"}.get(tier, "gray"))

left, right = st.columns([3, 2])
with left:
    st.markdown("**Brief for the Solution Engineer**")
    st.info(text(r.get("se_brief")) or "No brief recorded.")
    st.markdown("**Problem, in the visitor's words**")
    st.write(text(r.get("problem_statement")) or "-")
    st.markdown(f"**Blueprint:** {text(r.get('poc_name'))}  \n{text(r.get('poc_summary'))}")
    if text(r.get("document_url")):
        st.link_button("Open blueprint document", r["document_url"],
                       icon=":material/open_in_new:")
with right:
    with st.container(border=True):
        st.markdown("**Why the model scored it this way**")
        st.markdown(f"Capacity answer: **{text(scoring.get('capacity')) or '-'}**")
        st.markdown(f"Solvable: {text(scoring.get('solvable_why')) or '-'}")
        st.markdown(f"Consumption ({text(scoring.get('consumption_estimate')) or '-'}): "
                    f"{text(scoring.get('consumption_why')) or '-'}")
        if scoring.get("manual"):
            m = scoring["manual"]
            st.caption(f"Last edited by {m.get('by') or 'an SE'} at {m.get('at')}. "
                       f"Model scores were {m.get('model_scores')}.")
    st.markdown(f"Data they hold: {', '.join(map(str, as_list(r.get('data_held')))) or '-'}")
    st.markdown("Marketplace picks: "
                f"{', '.join(map(str, as_list(r.get('marketplace_joined')))) or '-'}")
    st.markdown(f"Game score: **{text(r.get('score')) or '-'}** - "
                f"bonus answered: {text(r.get('bonus_answered')) or '0'}")


def score_input(label, value, help_text):
    v = int(value) if value is not None and not pd.isna(value) else 3
    return st.segmented_control(label, [1, 2, 3, 4, 5], default=v, help=help_text,
                                key=f"{label}_{sid}")


with st.form(f"edit_{sid}"):
    st.markdown("### Edit")
    c1, c2, c3 = st.columns(3)
    with c1:
        now = score_input("Now", r.get("se_score_now"),
                          "Can they start soon: 5 this month ... 1 later / no capacity")
    with c2:
        solv = score_input("Solvable", r.get("se_score_solvable"),
                           "Snowflake-solvable and a quick win with CoCo")
    with c3:
        cons = score_input("Consumption", r.get("se_score_consumption"),
                           "5 $100k+, 4 $50-100k, 3 $20-50k, 2 $5-20k, 1 under $5k")
    brief = st.text_area("Brief", text(r.get("se_brief")), height=140)
    notes = st.text_area("SE notes", text(r.get("notes")), height=90)
    with st.expander("Visitor inputs"):
        first = st.text_input("First name", text(r.get("first_name")))
        company = st.text_input("Company", text(r.get("company")))
        industry = st.text_input("Industry", text(r.get("industry")))
        problem = st.text_area("Problem statement", text(r.get("problem_statement")))
        poc_name = st.text_input("Blueprint name", text(r.get("poc_name")))
    saved = st.form_submit_button("Save changes", type="primary",
                                  icon=":material/save:")

if saved:
    if None in (now, solv, cons):
        st.error("Pick a value for all three scores.")
        st.stop()
    total = now + solv + cons
    new_tier = tier_for(total, pass_t, blue_t)
    manual = {
        "edited": True,
        "by": operator or None,
        "at": pd.Timestamp.utcnow().isoformat(timespec="seconds"),
        "model_scores": (scoring.get("manual") or {}).get("model_scores") or {
            "now": r.get("se_score_now"), "solvable": r.get("se_score_solvable"),
            "consumption": r.get("se_score_consumption"), "tier": tier},
    }
    for k, v in list(manual["model_scores"].items()):
        if v is not None and not isinstance(v, str) and pd.isna(v):
            manual["model_scores"][k] = None
        elif hasattr(v, "item"):
            manual["model_scores"][k] = v.item()
    try:
        run(f"""
            update {TABLE} set
              se_score_now = ?, se_score_solvable = ?, se_score_consumption = ?,
              se_score_avg = round(? / 3, 1), se_qualified = ?,
              se_scoring = object_insert(object_insert(coalesce(se_scoring, object_construct()),
                             'tier', ?, true), 'manual', parse_json(?), true),
              se_brief = ?, notes = ?, se_operator = coalesce(nullif(?, ''), se_operator),
              first_name = ?, company = ?, industry = ?, problem_statement = ?, poc_name = ?
            where session_id = ?""",
            [now, solv, cons, float(total), new_tier == "qualified", new_tier,
             json.dumps(manual), brief, notes, operator, first, company, industry,
             problem, poc_name, sid])
    except Exception as e:  # noqa: BLE001
        st.error(f"Save failed: {e}")
        st.stop()
    st.toast(f"Saved - now {TIER_LABEL[new_tier]} (avg {round(total / 3, 1)})",
             icon=":material/check_circle:")
    st.rerun()
