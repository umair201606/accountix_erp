from datetime import datetime, date
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from ..extensions import db
from shared.tenancy import scoped_get_404
from ..models.workplace import Announcement, TeamEvent, KanbanBoard, KanbanTask

workplace_bp = Blueprint("workplace", __name__, url_prefix="/workplace")


@workplace_bp.route("/")
@login_required
def index():
    announcements = Announcement.query.order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).all()
    events = TeamEvent.query.filter(TeamEvent.event_date >= date.today()).order_by(TeamEvent.event_date).limit(10).all()
    boards = KanbanBoard.query.order_by(KanbanBoard.created_at.desc()).all()
    return render_template("workplace/index.html", announcements=announcements, events=events, boards=boards)


@workplace_bp.route("/announcements")
@login_required
def announcements():
    all_announcements = Announcement.query.order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).all()
    return render_template("workplace/announcements.html", announcements=all_announcements)


@workplace_bp.route("/announcements/create", methods=["POST"])
@login_required
def create_announcement():
    if not current_user.has_permission("workplace", "write"):
        return jsonify({"error": "Access denied"}), 403
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    priority = request.form.get("priority", "normal")
    if not title or not content:
        flash("Title and content are required.", "danger")
        return redirect(url_for("workplace.index"))
    ann = Announcement(title=title, content=content, priority=priority, author_id=current_user.id)
    db.session.add(ann)
    db.session.commit()
    flash("Announcement published.", "success")
    return redirect(url_for("workplace.index"))


@workplace_bp.route("/events")
@login_required
def events():
    all_events = TeamEvent.query.order_by(TeamEvent.event_date).all()
    return render_template("workplace/events.html", events=all_events)


@workplace_bp.route("/events/create", methods=["POST"])
@login_required
def create_event():
    title = request.form.get("title", "").strip()
    event_date = request.form.get("event_date")
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")
    location = request.form.get("location", "").strip()
    event_type = request.form.get("event_type", "meeting")
    if not title or not event_date:
        flash("Title and date are required.", "danger")
        return redirect(url_for("workplace.index"))
    evt = TeamEvent(
        title=title, event_date=datetime.strptime(event_date, "%Y-%m-%d").date(),
        location=location, event_type=event_type, created_by=current_user.id
    )
    if start_time:
        evt.start_time = datetime.strptime(start_time, "%H:%M").time()
    if end_time:
        evt.end_time = datetime.strptime(end_time, "%H:%M").time()
    db.session.add(evt)
    db.session.commit()
    flash("Event created.", "success")
    return redirect(url_for("workplace.index"))


@workplace_bp.route("/kanban")
@login_required
def kanban():
    boards = KanbanBoard.query.all()
    return render_template("workplace/kanban.html", boards=boards)


@workplace_bp.route("/kanban/create-board", methods=["POST"])
@login_required
def create_board():
    title = request.form.get("title", "").strip()
    if not title:
        return jsonify({"error": "Title required"}), 400
    board = KanbanBoard(title=title, created_by=current_user.id)
    db.session.add(board)
    db.session.commit()
    return jsonify({"success": True, "id": board.id, "title": board.title})


@workplace_bp.route("/kanban/<int:bid>/add-task", methods=["POST"])
@login_required
def add_task(bid):
    board = scoped_get_404(KanbanBoard, bid)
    title = request.form.get("title", "").strip()
    assignee_id = request.form.get("assignee_id", type=int)
    if not title:
        return jsonify({"error": "Title required"}), 400
    task = KanbanTask(board_id=bid, title=title, assignee_id=assignee_id)
    db.session.add(task)
    db.session.commit()
    return jsonify({"success": True, "id": task.id})


@workplace_bp.route("/kanban/<int:bid>/tasks")
@login_required
def board_tasks(bid):
    tasks = KanbanTask.query.filter_by(board_id=bid).order_by(KanbanTask.position).all()
    return jsonify([{
        "id": t.id, "title": t.title, "status": t.status,
        "priority": t.priority, "assignee": t.assignee.full_name if t.assignee else None,
        "due_date": t.due_date.isoformat() if t.due_date else None
    } for t in tasks])


@workplace_bp.route("/kanban/update-task/<int:tid>", methods=["POST"])
@login_required
def update_task(tid):
    task = scoped_get_404(KanbanTask, tid)
    data = request.get_json()
    if "status" in data:
        task.status = data["status"]
    if "position" in data:
        task.position = data["position"]
    db.session.commit()
    return jsonify({"success": True})


@workplace_bp.route("/kanban/delete-task/<int:tid>", methods=["POST"])
@login_required
def delete_task(tid):
    task = scoped_get_404(KanbanTask, tid)
    db.session.delete(task)
    db.session.commit()
    return jsonify({"success": True})
