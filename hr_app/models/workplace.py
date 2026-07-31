from datetime import datetime, date
from ..extensions import db


class Announcement(db.Model):
    __tablename__ = "announcements"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(20), default="normal")
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    pinned = db.Column(db.Boolean, default=False)
    expires_at = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    author = db.relationship("User", backref="announcements")


class TeamEvent(db.Model):
    __tablename__ = "team_events"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    event_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time)
    end_time = db.Column(db.Time)
    location = db.Column(db.String(200))
    event_type = db.Column(db.String(50), default="meeting")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship("User", backref="events")


class KanbanBoard(db.Model):
    __tablename__ = "kanban_boards"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship("User", backref="kanban_boards")
    tasks = db.relationship("KanbanTask", backref="board", lazy="dynamic", cascade="all, delete-orphan")


class KanbanTask(db.Model):
    __tablename__ = "kanban_tasks"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    board_id = db.Column(db.Integer, db.ForeignKey("kanban_boards.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(30), default="todo")
    priority = db.Column(db.String(20), default="medium")
    assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    due_date = db.Column(db.Date)
    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assignee = db.relationship("User", backref="kanban_tasks")

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "status": self.status,
            "priority": self.priority,
            "assignee": self.assignee.full_name if self.assignee else None,
            "due_date": self.due_date.isoformat() if self.due_date else None
        }
