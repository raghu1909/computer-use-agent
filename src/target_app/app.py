"""Mock legacy core-banking back-office UI.

Deliberately hostile to automation: table layout, <font> tags, framesets, no
<label for>, no ids/test-ids, generic class names. Stands in for a vendor
product with no API.

Error injection: request any page with ``?force_error=<code>`` and the code is
armed in the session; it fires once at the route that owns it, then clears.

    session_timeout    -> member search redirects to a "Session Expired" interstitial
    maintenance_notice -> member detail is covered by a dismissible notice
    permission_denied  -> opening a sub-account is refused
    slow_load          -> member detail takes ~4s to render
    app_error          -> member detail returns HTTP 500
    member_not_found   -> (no injection needed: use a member number that does not exist)
"""

from __future__ import annotations

import os
import time

from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("TARGET_APP_SECRET", "not-a-real-secret-local-demo-only")

MEMBERS: dict[str, dict] = {
    "12345": {
        "number": "12345",
        "name": "JANE Q PUBLIC",
        "since": "03/14/2009",
        "savings": "4,812.37",
        "checking": "1,203.90",
        "status": "ACTIVE",
        "accounts": [("S01", "Regular Savings", "4,812.37"), ("D01", "Checking", "1,203.90")],
    },
    "23456": {
        "number": "23456",
        "name": "ROBERT L MARTIN",
        "since": "11/02/2015",
        "savings": "250.00",
        "checking": "0.00",
        "status": "ACTIVE",
        "accounts": [("S01", "Regular Savings", "250.00")],
    },
    "34567": {
        "number": "34567",
        "name": "ALICE M CHEN",
        "since": "06/21/2001",
        "savings": "18,905.12",
        "checking": "3,377.45",
        "status": "DORMANT",
        "accounts": [
            ("S01", "Regular Savings", "18,905.12"),
            ("D01", "Checking", "3,377.45"),
            ("C02", "Holiday Club", "600.00"),
        ],
    },
}
ACCOUNT_TYPES = ["Holiday Club", "Money Market", "Youth Savings", "Christmas Club"]
CONFIRMATION_COUNTER = {"n": 88100}


@app.before_request
def arm_forced_error() -> None:
    code = request.args.get("force_error")
    if code:
        session["force_error"] = code


def take_error(*codes: str) -> str | None:
    armed = session.get("force_error")
    if armed in codes:
        session.pop("force_error", None)
        return armed
    return None


def logged_in() -> bool:
    return bool(session.get("user"))


@app.get("/")
def login_page():
    return render_template("login.html", error=None)


@app.post("/login")
def login():
    u, p = request.form.get("userid", ""), request.form.get("passwd", "")
    if u == "operator" and p == "letmein-demo":
        session["user"] = u
        return redirect(url_for("members"))
    return render_template("login.html", error="INVALID USER ID OR PASSWORD"), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.get("/members")
def members():
    if not logged_in():
        return redirect(url_for("login_page"))
    return render_template("members.html", error=None, query="")


@app.post("/members/search")
def members_search():
    if not logged_in():
        return redirect(url_for("login_page"))
    if take_error("session_timeout"):
        session.pop("user", None)
        return render_template("timeout.html")
    q = request.form.get("membernum", "").strip()
    if q in MEMBERS:
        return redirect(url_for("member_frameset", member_id=q))
    return render_template("members.html", error=f"NO MEMBER FOUND FOR NUMBER {q or '(BLANK)'}", query=q), 200


@app.get("/session/continue")
def session_continue():
    session["user"] = "operator"
    return redirect(url_for("members"))


@app.get("/members/<member_id>")
def member_frameset(member_id: str):
    if not logged_in():
        return redirect(url_for("login_page"))
    if member_id not in MEMBERS:
        return render_template("members.html", error=f"NO MEMBER FOUND FOR NUMBER {member_id}", query=member_id)
    return render_template("frameset.html", m=MEMBERS[member_id])


@app.get("/members/<member_id>/nav")
def member_nav(member_id: str):
    return render_template("nav.html", m=MEMBERS[member_id])


@app.get("/members/<member_id>/detail")
def member_detail(member_id: str):
    err = take_error("slow_load", "app_error", "maintenance_notice")
    if err == "slow_load":
        time.sleep(4)
    if err == "app_error":
        return render_template("error500.html"), 500
    return render_template("detail.html", m=MEMBERS[member_id], notice=(err == "maintenance_notice"))


@app.get("/members/<member_id>/accounts/new")
def account_new(member_id: str):
    return render_template("account_new.html", m=MEMBERS[member_id], types=ACCOUNT_TYPES, error=None)


@app.post("/members/<member_id>/accounts/new")
def account_create(member_id: str):
    m = MEMBERS[member_id]
    if take_error("permission_denied"):
        return render_template(
            "account_new.html",
            m=m,
            types=ACCOUNT_TYPES,
            error="PERMISSION DENIED - OPERATOR NOT AUTHORIZED FOR ACCOUNT OPENING",
        ), 403
    acct_type = request.form.get("accttype", "")
    deposit = request.form.get("deposit", "").strip()
    if acct_type not in ACCOUNT_TYPES:
        return render_template(
            "account_new.html", m=m, types=ACCOUNT_TYPES, error="VALIDATION ERROR - ACCOUNT TYPE IS REQUIRED"
        ), 400
    try:
        amount = float(deposit)
        if amount < 5:
            raise ValueError
    except ValueError:
        return render_template(
            "account_new.html",
            m=m,
            types=ACCOUNT_TYPES,
            error="VALIDATION ERROR - OPENING DEPOSIT MUST BE AT LEAST 5.00",
        ), 400
    CONFIRMATION_COUNTER["n"] += 1
    return render_template(
        "confirmation.html",
        m=m,
        acct_type=acct_type,
        deposit=f"{amount:,.2f}",
        confirmation=f"CNF-{CONFIRMATION_COUNTER['n']}",
    )


def main() -> None:
    port = int(os.environ.get("TARGET_APP_PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
