"""
Flask + SQLite backend for the Annual Review slide deck.

Serves the presentation frontend and a small REST API that persists the deck
(metadata + ordered slides) in a SQLite database via SQLAlchemy.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000

Data model
----------
Meta   : a single row (id=1) holding the deck-wide header text.
Slide  : one row per slide, ordered by `position`. Slide-type specific
         content (paras / items / head / rows / chart / …) is stored as a
         JSON blob in `content`, which keeps the schema flexible while the
         core fields (position / type / title) stay first-class columns.
"""
import json
import os

from flask import Flask, request, jsonify, send_file, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "deck.db")
SEED_PATH = os.path.join(BASE, "seed_deck.json")
FRONTEND = os.path.join(BASE, "presentation.html")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + DB_PATH.replace("\\", "/")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JSON_AS_ASCII"] = False  # keep Devanagari readable in responses
db = SQLAlchemy(app)


# --------------------------------------------------------------------------- #
#  Models
# --------------------------------------------------------------------------- #
META_FIELDS = ["province", "ministry", "office", "officeFull", "fy", "reportTitle"]


class Meta(db.Model):
    """Deck-wide header metadata. Kept as a singleton row (id == 1)."""
    __tablename__ = "meta"
    id = db.Column(db.Integer, primary_key=True)
    province = db.Column(db.Text, default="")
    ministry = db.Column(db.Text, default="")
    office = db.Column(db.Text, default="")
    officeFull = db.Column(db.Text, default="")
    fy = db.Column(db.Text, default="")
    reportTitle = db.Column(db.Text, default="")

    def to_dict(self):
        return {k: (getattr(self, k) or "") for k in META_FIELDS}

    def update(self, data):
        for k in META_FIELDS:
            if k in data and data[k] is not None:
                setattr(self, k, data[k])


class Slide(db.Model):
    """A single slide. Flexible, type-specific data lives in `content` (JSON)."""
    __tablename__ = "slides"
    id = db.Column(db.Integer, primary_key=True)
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
        """Populate columns + content JSON from a full slide dict."""
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


# --------------------------------------------------------------------------- #
#  Serialization / seeding helpers
# --------------------------------------------------------------------------- #
def deck_dict():
    meta = db.session.get(Meta, 1)
    slides = Slide.query.order_by(Slide.position).all()
    return {
        "meta": meta.to_dict() if meta else {k: "" for k in META_FIELDS},
        "slides": [s.to_dict() for s in slides],
    }


def _load_deck(data):
    """Replace all rows in the DB with the given deck dict."""
    Slide.query.delete()
    Meta.query.delete()
    meta = Meta(id=1)
    meta.update(data.get("meta", {}))
    db.session.add(meta)
    for i, sl in enumerate(data.get("slides", [])):
        s = Slide(position=i)
        s.set_from(sl)
        db.session.add(s)
    db.session.commit()


def seed_if_empty(force=False):
    if not force and Slide.query.first() is not None:
        return
    with open(SEED_PATH, encoding="utf-8") as fh:
        _load_deck(json.load(fh))


def _shift(from_pos, delta):
    """Shift positions of all slides at/after `from_pos` by `delta`."""
    Slide.query.filter(Slide.position >= from_pos).update(
        {Slide.position: Slide.position + delta}, synchronize_session=False)


# --------------------------------------------------------------------------- #
#  Routes — frontend
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_file(FRONTEND)


@app.route("/admin")
def admin():
    return send_file(os.path.join(BASE, "admin.html"))


# --------------------------------------------------------------------------- #
#  Routes — API
# --------------------------------------------------------------------------- #
@app.get("/api/deck")
def api_get_deck():
    return jsonify(deck_dict())


@app.put("/api/meta")
def api_update_meta():
    meta = db.session.get(Meta, 1) or Meta(id=1)
    meta.update(request.get_json(force=True) or {})
    db.session.add(meta)
    db.session.commit()
    return jsonify(meta.to_dict())


@app.post("/api/slides")
def api_add_slide():
    body = request.get_json(force=True) or {}
    slide = body.get("slide") or {}
    after = body.get("after")  # id of the slide to insert *after*, or None

    if after is None:
        pos = (db.session.query(func.max(Slide.position)).scalar() or -1) + 1
    else:
        ref = db.session.get(Slide, after)
        pos = (ref.position + 1) if ref else 0
        _shift(pos, +1)

    s = Slide(position=pos)
    s.set_from(slide)
    db.session.add(s)
    db.session.commit()
    return jsonify(s.to_dict()), 201


@app.put("/api/slides/<int:sid>")
def api_update_slide(sid):
    s = db.session.get(Slide, sid) or abort(404)
    s.set_from(request.get_json(force=True) or {})
    db.session.commit()
    return jsonify(s.to_dict())


@app.delete("/api/slides/<int:sid>")
def api_delete_slide(sid):
    s = db.session.get(Slide, sid) or abort(404)
    pos = s.position
    db.session.delete(s)
    _shift(pos + 1, -1)
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/slides/<int:sid>/duplicate")
def api_duplicate_slide(sid):
    s = db.session.get(Slide, sid) or abort(404)
    pos = s.position + 1
    _shift(pos, +1)
    clone = Slide(position=pos, type=s.type, title=s.title, content=s.content)
    db.session.add(clone)
    db.session.commit()
    return jsonify(clone.to_dict()), 201


@app.post("/api/slides/reorder")
def api_reorder():
    order = (request.get_json(force=True) or {}).get("order", [])
    for i, sid in enumerate(order):
        Slide.query.filter_by(id=sid).update({Slide.position: i},
                                             synchronize_session=False)
    db.session.commit()
    return jsonify(deck_dict())


@app.put("/api/deck")
def api_save_deck():
    """Bulk 'save all': update meta + upsert every slide in one transaction,
    preserving existing ids, re-applying order, and dropping removed slides."""
    data = request.get_json(force=True) or {}
    meta = db.session.get(Meta, 1) or Meta(id=1)
    meta.update(data.get("meta", {}))
    db.session.add(meta)

    incoming = data.get("slides", [])
    # Safety: never let a meta-only / empty payload delete every slide.
    if not incoming:
        db.session.commit()
        return jsonify(deck_dict())

    existing = {s.id: s for s in Slide.query.all()}
    keep_ids = {sl.get("_id") for sl in incoming if sl.get("_id") is not None}
    for sid, s in existing.items():
        if sid not in keep_ids:
            db.session.delete(s)

    for i, sl in enumerate(incoming):
        s = existing.get(sl.get("_id"))
        if s is None:
            s = Slide()
            db.session.add(s)
        s.position = i
        s.set_from(sl)

    db.session.commit()
    return jsonify(deck_dict())


@app.post("/api/import")
def api_import():
    _load_deck(request.get_json(force=True) or {})
    return jsonify(deck_dict())


@app.post("/api/reset")
def api_reset():
    seed_if_empty(force=True)
    return jsonify(deck_dict())


# --------------------------------------------------------------------------- #
#  Bootstrap
# --------------------------------------------------------------------------- #
with app.app_context():
    db.create_all()
    seed_if_empty()


if __name__ == "__main__":
    # use_reloader=False: the auto-reloader spawns a child process on every
    # file change, which can leave orphaned servers bound to the port.
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
