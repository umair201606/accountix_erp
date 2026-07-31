from datetime import datetime, date
from ..extensions import db


class Project(db.Model):
    __tablename__ = "projects"
    __table_args__ = (
        db.UniqueConstraint("company_id", "code",
                            name="uq_projects_code"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(200), nullable=False)
    code = db.Column(db.String(20))
    description = db.Column(db.Text)
    department = db.Column(db.String(100))
    project_manager_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="active")
    is_billable = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    project_manager = db.relationship("User", backref="managed_projects")
    work_packages = db.relationship("WorkPackage", backref="project", lazy="dynamic", cascade="all, delete-orphan")


class WorkPackage(db.Model):
    __tablename__ = "work_packages"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    code = db.Column(db.String(20))
    description = db.Column(db.Text)
    estimated_hours = db.Column(db.Float)
    status = db.Column(db.String(20), default="active")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tasks = db.relationship("ProjectTask", backref="work_package", lazy="dynamic", cascade="all, delete-orphan")


class ProjectTask(db.Model):
    __tablename__ = "project_tasks"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    work_package_id = db.Column(db.Integer, db.ForeignKey("work_packages.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    estimated_hours = db.Column(db.Float)
    assigned_to = db.Column(db.Integer, db.ForeignKey("users.id"))
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assignee = db.relationship("User", backref="project_tasks")
