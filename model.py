# models.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy import func
from enum import Enum

db = SQLAlchemy()

class Role(Enum):
    ADMIN = 'admin'
    MANAGER = 'manager'
    EMPLOYEE = 'employee'

class Status(Enum):
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'

class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    currency = db.Column(db.String(3), nullable=False)
    country = db.Column(db.String(100), nullable=False)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.Enum('admin', 'manager', 'employee'), nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    company = db.relationship('Company', backref='users')
    manager = db.relationship('User', remote_side=[id], backref='direct_subordinates')

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount_original = db.Column(db.Float, nullable=False)
    currency_original = db.Column(db.String(3), nullable=False, default='USD')
    amount_converted = db.Column(db.Float)
    category = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text)
    date = db.Column(db.Date, nullable=False)
    status = db.Column(db.Enum('pending', 'approved', 'rejected'), default='pending')
    comments = db.Column(db.Text)
    receipt_url = db.Column(db.String(200))
    employee = db.relationship('User', backref='expenses')

class Approval(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expense_id = db.Column(db.Integer, db.ForeignKey('expense.id'), nullable=False)
    approver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    step = db.Column(db.Integer, default=1)
    action = db.Column(db.Enum('approved', 'rejected'))
    comments = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=func.now())
    approver = db.relationship('User')
    expense = db.relationship('Expense', backref='approvals')

class Workflow(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    config = db.Column(db.JSON, default=lambda: {
        'type': 'sequential',
        'steps': [
            {'type': 'manager_of_submitter'},
            {'type': 'role', 'role': 'admin'}
        ],
        'conditional': None
    })
    company = db.relationship('Company', backref='workflows')
