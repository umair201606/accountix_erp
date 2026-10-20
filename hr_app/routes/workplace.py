from datetime import datetime, date
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from ..extensions import db
from shared.tenancy import scoped_get_404, get_member
from ..models.workplace import Announcement, TeamEvent, KanbanBoard, KanbanTask

workplace_bp = Blueprint("workplace", __name__, url_prefix="/workplace")

TASK_STATUSES = ("todo", "in_progress", "done")


def _can_post():
    """Company-wide posts (announcements, calendar events): an admin or a
    manager by the role held in THIS company. The old check read the global
    role's permission table, so a manager here who was an employee elsewhere
    could not post, and an employee here who was a manager elsewhere could."""
    return current_user.is_admin() or current_user.is_manager()


def _can_manage_board(board):
    """Add or remove a board's tasks: its owner, a manager or an admin.
    Anyone in the company could delete any task on any board."""
    return (board.created_by == current_user.id or current_user.is_admin()
            or current_user.is_manager())


def _can_move(task):
    """Move a task across the board: whoever may manage the board, or the
    person it is assigned to."""
    return _can_manage_board(task.board) or task.assignee_id == current_user.id


def _task_json(t):
    return {
        "id": t.id, "title": t.title, "status": t.status,
        "priority": t.priority, "assignee": t.assignee.full_name if t.assignee else None,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "can_move": _can_move(t), "can_delete": _can_manage_board(t.board),
    }


@workplace_bp.route("/")
@login_required
def index():
    announcements = Announcement.query.order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).all()
    events = TeamEvent.query.filter(TeamEvent.event_date >= date.today()).order_by(TeamEvent.event_date).limit(10).all()
    boards = KanbanBoard.query.order_by(KanbanBoard.created_at.desc()).all()
    return render_template("workplace/index.html", announcements=announcements, events=events, boards=boards,
                           can_post=_can_post())


@workplace_bp.route("/announcements")
@login_required
def announcements():
    all_announcements = Announcement.query.order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).all()
    return render_template("workplace/announcements.html", announcements=all_announcements)


@workplace_bp.route("/announcements/create", methods=["POST"])
@login_required
def create_announcement():
    if not _can_post():
        flash("Only a manager or an admin can publish announcements.", "danger")
        return redirect(url_for("workplace.index"))
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    priority = request.form.get("priority", "normal")
    if priority not in ("normal", "high", "urgent"):
        priority = "normal"
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
    return render_template("workplace/events.html", events=all_events, can_post=_can_post())


@workplace_bp.route("/events/create", methods=["POST"])
@login_required
def create_event():
    if not _can_post():
        flash("Only a manager or an admin can add events to the team calendar.", "danger")
        return redirect(url_for("workplace.events"))
    title = request.form.get("title", "").strip()
    event_date = request.form.get("event_date")
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")
    location = request.form.get("location", "").strip()
    event_type = request.form.get("event_type", "meeting")
    if not title or not event_date:
        flash("Title and date are required.", "danger")
        return redirect(url_for("workplace.index"))
    if event_type not in ("meeting", "workshop", "social", "other"):
        event_type = "other"
    try:
        evt = TeamEvent(
            title=title, event_date=datetime.strptime(event_date, "%Y-%m-%d").date(),
            location=location, event_type=event_type, created_by=current_user.id
        )
        if start_time:
            evt.start_time = datetime.strptime(start_time, "%H:%M").time()
        if end_time:
            evt.end_time = datetime.strptime(end_time, "%H:%M").time()
    except ValueError:
        flash("Enter the date as YYYY-MM-DD and times as HH:MM.", "danger")
        return redirect(url_for("workplace.events"))
    db.session.add(evt)
    db.session.commit()
    flash("Event created.", "success")
    return redirect(url_for("workplace.index"))


@workplace_bp.route("/kanban")
@login_required
def kanban():
    boards = KanbanBoard.query.all()
    manageable = {b.id for b in boards if _can_manage_board(b)}
    return render_template("workplace/kanban.html", boards=boards, manageable=manageable)


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
    if not _can_manage_board(board):
        return jsonify({"error": "Only the board's owner, a manager or an admin can add tasks."}), 403
    title = request.form.get("title", "").strip()
    assignee_id = request.form.get("assignee_id", type=int)
    if not title:
        return jsonify({"error": "Title required"}), 400
    if assignee_id and get_member(assignee_id) is None:
        # A task could be handed to anyone's id, from any company.
        return jsonify({"error": "Assign the task to a member of this company."}), 400
    task = KanbanTask(board_id=bid, title=title, assignee_id=assignee_id)
    db.session.add(task)
    db.session.commit()
    return jsonify({"success": True, "id": task.id})


@workplace_bp.route("/kanban/<int:bid>/tasks")
@login_required
def board_tasks(bid):
    scoped_get_404(KanbanBoard, bid)
    tasks = KanbanTask.query.filter_by(board_id=bid).order_by(KanbanTask.position).all()
    return jsonify([_task_json(t) for t in tasks])


@workplace_bp.route("/kanban/update-task/<int:tid>", methods=["POST"])
@login_required
def update_task(tid):
    task = scoped_get_404(KanbanTask, tid)
    if not _can_move(task):
        return jsonify({"error": "Only the task's assignee, the board's owner, a manager or an admin can move it."}), 403
    data = request.get_json(silent=True) or {}
    if "status" in data:
        if data["status"] not in TASK_STATUSES:
            return jsonify({"error": "Unknown status"}), 400
        task.status = data["status"]
    if "position" in data:
        try:
            task.position = int(data["position"])
        except (TypeError, ValueError):
            return jsonify({"error": "Position must be a number"}), 400
    db.session.commit()
    return jsonify({"success": True})


@workplace_bp.route("/kanban/delete-task/<int:tid>", methods=["POST"])
@login_required
def delete_task(tid):
    task = scoped_get_404(KanbanTask, tid)
    if not _can_manage_board(task.board):
        return jsonify({"error": "Only the board's owner, a manager or an admin can delete tasks."}), 403
    db.session.delete(task)
    db.session.commit()
    return jsonify({"success": True})
