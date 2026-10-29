"""
Flask + SQLite backend for the multi-user slide-deck app.

Each user owns their own decks. A deck can be marked public, which lists it in
the gallery and lets anyone view it read-only; editing always requires being
the signed-in owner.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000

Data model
----------
User   : an account (username / email / password hash).
Deck   : a presentation owned by a user (or ownerless demo). Holds the deck-wide
         header metadata + theme, a title, and a public flag.
Slide  : one row per slide, scoped to a deck and ordered by `position`. Type-
         specific content lives in the `content` JSON blob.
Image  : an uploaded image blob (optionally owned by a user).
"""
import base64
import binascii
import json
import os

from flask import Flask, request, jsonify, send_file, abort, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
SEED_PATH = os.path.join(BASE, "seed_deck.json")
FRONTEND = os.path.join(BASE, "presentation.html")


def _database_uri():
    """Pick the database.

    In production set DATABASE_URL to a managed database (e.g. the PostgreSQL
    service Railway provides) — its data lives outside the container, so it
    survives every deploy. Locally, with no DATABASE_URL, we fall back to a
    SQLite file (overridable via DATABASE_PATH).
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # Railway/Heroku hand out "postgres://…"; SQLAlchemy wants the
        # explicit driver scheme "postgresql+psycopg2://…".
        if url.startswith("postgres://"):
            url = "postgresql+psycopg2://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        return url
    db_path = os.environ.get("DATABASE_PATH", os.path.join(BASE, "deck.db"))
    return "sqlite:///" + db_path.replace("\\", "/")


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = _database_uri()
# Recycle stale pooled connections (managed DBs drop idle ones) and check
# liveness before use, so a dropped connection doesn't surface as a 500.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JSON_AS_ASCII"] = False  # keep Devanagari readable in responses
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
db = SQLAlchemy(app)

META_FIELDS = ["province", "ministry", "office", "officeFull", "fy", "reportTitle", "theme"]


# --------------------------------------------------------------------------- #
#  Models
# --------------------------------------------------------------------------- #
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.Text, nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())

    def public_dict(self):
        return {"id": self.id, "username": self.username, "email": self.email}


class Deck(db.Model):
    __tablename__ = "decks"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)  # NULL = demo
    title = db.Column(db.Text, default="")
    is_public = db.Column(db.Boolean, default=False, nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())
    updated = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now())
    # deck-wide header metadata
    province = db.Column(db.Text, default="")
    ministry = db.Column(db.Text, default="")
    office = db.Column(db.Text, default="")
    officeFull = db.Column(db.Text, default="")
    fy = db.Column(db.Text, default="")
    reportTitle = db.Column(db.Text, default="")
    theme = db.Column(db.Text, default="classic")

    def display_title(self):
        return (self.title or self.reportTitle or "शीर्षकविहीन प्रस्तुति").strip()

    def meta_dict(self):
        return {k: (getattr(self, k) or "") for k in META_FIELDS}

    def update_meta(self, data):
        for k in META_FIELDS:
            if k in data and data[k] is not None:
                setattr(self, k, data[k])

    def slides_ordered(self):
        return Slide.query.filter_by(deck_id=self.id).order_by(Slide.position).all()

    def summary(self, owner_name=None):
        return {
            "id": self.id,
            "title": self.display_title(),
            "is_public": bool(self.is_public),
            "owner": owner_name,
            "owner_id": self.owner_id,
            "slides": Slide.query.filter_by(deck_id=self.id).count(),
            "updated": self.updated.isoformat() if self.updated else None,
        }

    def full_dict(self, can_edit, owner_name=None):
        return {
            "id": self.id,
            "title": self.display_title(),
            "is_public": bool(self.is_public),
            "owner": owner_name,
            "owner_id": self.owner_id,
            "can_edit": bool(can_edit),
            "meta": self.meta_dict(),
            "slides": [s.to_dict() for s in self.slides_ordered()],
        }


class Slide(db.Model):
    __tablename__ = "slides"
    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(db.Integer, db.ForeignKey("decks.id"), index=True)
    position = db.Column(db.Integer, nullable=False, default=0, index=True)
    type = db.Column(db.String(40), nullable=False, default="list")
    title = db.Column(db.Text, default="")
    content = db.Column(db.Text, default="{}")

    def get_content(self):
        try:
            return json.loads(self.content or "{}")
        except (ValueError, TypeError):
            return {}

    def set_from(self, data):
        self.type = data.get("type", self.type or "list")
        self.title = data.get("title") or ""
        rest = {k: v for k, v in data.items()
                if k not in ("type", "title", "_id", "position")}
        self.content = json.dumps(rest, ensure_ascii=False)

    def to_dict(self):
        d = self.get_content()
        d["_id"] = self.id
        d["type"] = self.type
        if self.title:
            d["title"] = self.title
        return d


class Image(db.Model):
    __tablename__ = "images"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    filename = db.Column(db.Text, default="")
    mimetype = db.Column(db.String(80), default="image/png")
    data = db.Column(db.LargeBinary, nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())

    def url(self):
        return "/api/images/%d" % self.id

    def meta_dict(self):
        return {
            "id": self.id,
            "url": self.url(),
            "filename": self.filename or "",
            "mimetype": self.mimetype or "",
            "size": len(self.data or b""),
            "created": self.created.isoformat() if self.created else None,
        }


# --------------------------------------------------------------------------- #
#  Auth helpers
# --------------------------------------------------------------------------- #
def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None


def owner_name_of(deck):
    if deck.owner_id is None:
        return None
    u = db.session.get(User, deck.owner_id)
    return u.username if u else None


def require_login():
    u = current_user()
    if not u:
        abort(401, "login required")
    return u


def get_viewable_deck(did):
    """A deck the caller may VIEW: public, or owned by the caller."""
    deck = db.session.get(Deck, did) or abort(404)
    u = current_user()
    if deck.is_public or (u and deck.owner_id == u.id):
        return deck
    abort(404)  # hide existence of private decks


def get_owned_deck(did):
    """A deck the caller may EDIT: must be the signed-in owner."""
    u = require_login()
    deck = db.session.get(Deck, did) or abort(404)
    if deck.owner_id != u.id:
        abort(403, "not your presentation")
    return deck


def _shift(deck_id, from_pos, delta):
    Slide.query.filter(Slide.deck_id == deck_id, Slide.position >= from_pos).update(
        {Slide.position: Slide.position + delta}, synchronize_session=False)


def _touch(deck):
    deck.updated = func.now()


# --------------------------------------------------------------------------- #
#  Seeding
# --------------------------------------------------------------------------- #
def _new_deck_slides(deck, slides):
    for i, sl in enumerate(slides):
        s = Slide(deck_id=deck.id, position=i)
        s.set_from(sl)
        db.session.add(s)


def seed_demo_if_empty():
    """On a fresh DB (no decks), seed the bundled deck as an ownerless public demo."""
    if Deck.query.first() is not None:
        return
    with open(SEED_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    deck = Deck(owner_id=None, is_public=True)
    deck.update_meta(data.get("meta", {}))
    deck.title = deck.reportTitle or "नमुना प्रस्तुति"
    db.session.add(deck)
    db.session.flush()
    _new_deck_slides(deck, data.get("slides", []))
    db.session.commit()


# --------------------------------------------------------------------------- #
#  Routes — pages
# --------------------------------------------------------------------------- #
@app.route("/")
def gallery_page():
    return send_file(os.path.join(BASE, "gallery.html"))


@app.route("/view")
@app.route("/p/<int:did>")
def view_page(did=None):
    return send_file(FRONTEND)


@app.route("/admin")
def admin_page():
    return send_file(os.path.join(BASE, "admin.html"))


# --------------------------------------------------------------------------- #
#  Routes — auth
# --------------------------------------------------------------------------- #
@app.post("/api/auth/register")
def api_register():
    b = request.get_json(force=True) or {}
    username = (b.get("username") or "").strip()
    email = (b.get("email") or "").strip().lower()
    password = b.get("password") or ""
    if len(username) < 3 or "@" not in email or len(password) < 6:
        abort(400, "username ≥3, valid email, password ≥6 required")
    if User.query.filter_by(username=username).first():
        abort(409, "username taken")
    if User.query.filter_by(email=email).first():
        abort(409, "email already registered")
    u = User(username=username, email=email,
             password_hash=generate_password_hash(password))
    db.session.add(u)
    db.session.commit()
    session["uid"] = u.id
    return jsonify(u.public_dict()), 201


@app.post("/api/auth/login")
def api_login():
    b = request.get_json(force=True) or {}
    ident = (b.get("username") or b.get("email") or "").strip()
    password = b.get("password") or ""
    u = (User.query.filter_by(username=ident).first()
         or User.query.filter_by(email=ident.lower()).first())
    if not u or not check_password_hash(u.password_hash, password):
        abort(401, "invalid credentials")
    session["uid"] = u.id
    return jsonify(u.public_dict())


@app.post("/api/auth/logout")
def api_logout():
    session.pop("uid", None)
    return ("", 204)


@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify({"user": u.public_dict() if u else None})


# --------------------------------------------------------------------------- #
#  Routes — decks
# --------------------------------------------------------------------------- #
@app.get("/api/public/decks")
def api_public_decks():
    decks = Deck.query.filter_by(is_public=True).order_by(Deck.updated.desc()).all()
    return jsonify([d.summary(owner_name_of(d)) for d in decks])


@app.get("/api/decks")
def api_my_decks():
    u = require_login()
    decks = Deck.query.filter_by(owner_id=u.id).order_by(Deck.updated.desc()).all()
    return jsonify([d.summary(u.username) for d in decks])


@app.post("/api/decks")
def api_create_deck():
    u = require_login()
    b = request.get_json(silent=True) or {}
    title = (b.get("title") or "नयाँ प्रस्तुति").strip()
    deck = Deck(owner_id=u.id, is_public=False, title=title, reportTitle=title)
    db.session.add(deck)
    db.session.flush()
    _new_deck_slides(deck, [
        {"type": "title"},
        {"type": "list", "title": "नयाँ सूची", "cols": 1, "big": True, "items": ["नयाँ बुँदा"]},
        {"type": "thanks", "text": "धन्यवाद !"},
    ])
    db.session.commit()
    return jsonify(deck.summary(u.username)), 201


@app.get("/api/decks/<int:did>")
def api_get_deck(did):
    deck = get_viewable_deck(did)
    u = current_user()
    can_edit = bool(u and deck.owner_id == u.id)
    return jsonify(deck.full_dict(can_edit, owner_name_of(deck)))


@app.put("/api/decks/<int:did>")
def api_update_deck(did):
    """Update deck settings (title / public flag) and/or meta."""
    deck = get_owned_deck(did)
    b = request.get_json(force=True) or {}
    if "title" in b and b["title"] is not None:
        deck.title = b["title"]
    if "is_public" in b:
        deck.is_public = bool(b["is_public"])
    if isinstance(b.get("meta"), dict):
        deck.update_meta(b["meta"])
    _touch(deck)
    db.session.commit()
    return jsonify(deck.summary(owner_name_of(deck)))


@app.delete("/api/decks/<int:did>")
def api_delete_deck(did):
    deck = get_owned_deck(did)
    Slide.query.filter_by(deck_id=deck.id).delete()
    db.session.delete(deck)
    db.session.commit()
    return ("", 204)


@app.put("/api/decks/<int:did>/meta")
def api_update_meta(did):
    deck = get_owned_deck(did)
    deck.update_meta(request.get_json(force=True) or {})
    _touch(deck)
    db.session.commit()
    return jsonify(deck.meta_dict())


# --------------------------------------------------------------------------- #
#  Routes — slides (scoped to a deck, owner only)
# --------------------------------------------------------------------------- #
@app.post("/api/decks/<int:did>/slides")
def api_add_slide(did):
    deck = get_owned_deck(did)
    body = request.get_json(force=True) or {}
    slide = body.get("slide") or {}
    after = body.get("after")
    if after is None:
        pos = (db.session.query(func.max(Slide.position))
               .filter(Slide.deck_id == deck.id).scalar() or -1) + 1
    else:
        ref = db.session.get(Slide, after)
        pos = (ref.position + 1) if ref and ref.deck_id == deck.id else 0
        _shift(deck.id, pos, +1)
    s = Slide(deck_id=deck.id, position=pos)
    s.set_from(slide)
    db.session.add(s)
    _touch(deck)
    db.session.commit()
    return jsonify(s.to_dict()), 201


def _deck_slide(deck, sid):
    s = db.session.get(Slide, sid)
    if not s or s.deck_id != deck.id:
        abort(404)
    return s


@app.put("/api/decks/<int:did>/slides/<int:sid>")
def api_update_slide(did, sid):
    deck = get_owned_deck(did)
    s = _deck_slide(deck, sid)
    s.set_from(request.get_json(force=True) or {})
    _touch(deck)
    db.session.commit()
    return jsonify(s.to_dict())


@app.delete("/api/decks/<int:did>/slides/<int:sid>")
def api_delete_slide(did, sid):
    deck = get_owned_deck(did)
    s = _deck_slide(deck, sid)
    pos = s.position
    db.session.delete(s)
    _shift(deck.id, pos + 1, -1)
    _touch(deck)
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/decks/<int:did>/slides/<int:sid>/duplicate")
def api_duplicate_slide(did, sid):
    deck = get_owned_deck(did)
    s = _deck_slide(deck, sid)
    pos = s.position + 1
    _shift(deck.id, pos, +1)
    clone = Slide(deck_id=deck.id, position=pos, type=s.type, title=s.title, content=s.content)
    db.session.add(clone)
    _touch(deck)
    db.session.commit()
    return jsonify(clone.to_dict()), 201


@app.post("/api/decks/<int:did>/slides/reorder")
def api_reorder(did):
    deck = get_owned_deck(did)
    order = (request.get_json(force=True) or {}).get("order", [])
    for i, sid in enumerate(order):
        Slide.query.filter_by(id=sid, deck_id=deck.id).update(
            {Slide.position: i}, synchronize_session=False)
    _touch(deck)
    db.session.commit()
    return jsonify(deck.full_dict(True, owner_name_of(deck)))


@app.put("/api/decks/<int:did>/save")
def api_save_deck(did):
    """Bulk save: meta + upsert every slide, preserving ids, order, deletions."""
    deck = get_owned_deck(did)
    data = request.get_json(force=True) or {}
    if isinstance(data.get("meta"), dict):
        deck.update_meta(data["meta"])
    incoming = data.get("slides", [])
    if not incoming:
        _touch(deck)
        db.session.commit()
        return jsonify(deck.full_dict(True, owner_name_of(deck)))
    existing = {s.id: s for s in Slide.query.filter_by(deck_id=deck.id).all()}
    keep = {sl.get("_id") for sl in incoming if sl.get("_id") is not None}
    for sid, s in existing.items():
        if sid not in keep:
            db.session.delete(s)
    for i, sl in enumerate(incoming):
        s = existing.get(sl.get("_id"))
        if s is None:
            s = Slide(deck_id=deck.id)
            db.session.add(s)
        s.position = i
        s.set_from(sl)
    _touch(deck)
    db.session.commit()
    return jsonify(deck.full_dict(True, owner_name_of(deck)))


@app.post("/api/decks/<int:did>/import")
def api_import(did):
    deck = get_owned_deck(did)
    data = request.get_json(force=True) or {}
    Slide.query.filter_by(deck_id=deck.id).delete()
    deck.update_meta(data.get("meta", {}))
    _new_deck_slides(deck, data.get("slides", []))
    _touch(deck)
    db.session.commit()
    return jsonify(deck.full_dict(True, owner_name_of(deck)))


# --------------------------------------------------------------------------- #
#  Routes — images
# --------------------------------------------------------------------------- #
@app.post("/api/images")
def api_upload_image():
    u = require_login()
    filename, mimetype, raw = "", "image/png", None
    if request.files.get("file"):
        f = request.files["file"]
        filename = f.filename or "upload"
        mimetype = f.mimetype or mimetype
        raw = f.read()
    else:
        body = request.get_json(silent=True) or {}
        filename = body.get("filename") or "upload"
        mimetype = body.get("mimetype") or mimetype
        b64 = body.get("data") or ""
        if "," in b64 and b64.strip().startswith("data:"):
            head, b64 = b64.split(",", 1)
            if ";" in head and ":" in head:
                mimetype = head.split(":", 1)[1].split(";", 1)[0] or mimetype
        try:
            raw = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            raw = None
    if not raw:
        abort(400, "no image data")
    im = Image(owner_id=u.id, filename=filename, mimetype=mimetype, data=raw)
    db.session.add(im)
    db.session.commit()
    return jsonify(im.meta_dict()), 201


@app.get("/api/images/<int:iid>")
def api_get_image(iid):
    im = db.session.get(Image, iid) or abort(404)
    return app.response_class(im.data, mimetype=im.mimetype or "image/png")


@app.delete("/api/images/<int:iid>")
def api_delete_image(iid):
    u = require_login()
    im = db.session.get(Image, iid)
    if im and (im.owner_id is None or im.owner_id == u.id):
        db.session.delete(im)
        db.session.commit()
    return ("", 204)


# --------------------------------------------------------------------------- #
#  Error handler -> JSON for API routes
# --------------------------------------------------------------------------- #
@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
def _json_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(e, "description", str(e)),
                        "status": getattr(e, "code", 500)}), getattr(e, "code", 500)
    return e


# --------------------------------------------------------------------------- #
#  Bootstrap + migration
# --------------------------------------------------------------------------- #
def _migrate():
    """Create new tables and fold any legacy single-deck data into a demo deck."""
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)

    def cols(table):
        try:
            return {c["name"] for c in insp.get_columns(table)}
        except Exception:
            return set()

    def add_col(table, ddl):
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except Exception:
            db.session.rollback()

    scols = cols("slides")
    if scols and "deck_id" not in scols:
        add_col("slides", "ALTER TABLE slides ADD COLUMN deck_id INTEGER")
    icols = cols("images")
    if icols and "owner_id" not in icols:
        add_col("images", "ALTER TABLE images ADD COLUMN owner_id INTEGER")

    # Fold legacy slides (no deck) into a new ownerless public demo deck,
    # carrying over the old singleton `meta` row if it exists.
    orphan = db.session.execute(
        text("SELECT COUNT(*) FROM slides WHERE deck_id IS NULL")).scalar() or 0
    if orphan:
        meta = {}
        if "meta" in insp.get_table_names():
            row = db.session.execute(text("SELECT * FROM meta WHERE id=1")).mappings().first()
            if row:
                meta = {k: row.get(k) for k in META_FIELDS if k in row}
        deck = Deck(owner_id=None, is_public=True)
        deck.update_meta(meta)
        deck.title = deck.reportTitle or "नमुना प्रस्तुति"
        db.session.add(deck)
        db.session.flush()
        db.session.execute(
            text("UPDATE slides SET deck_id=:d WHERE deck_id IS NULL"), {"d": deck.id})
        db.session.commit()


with app.app_context():
    db.create_all()
    _migrate()
    seed_demo_if_empty()


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)
