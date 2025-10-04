from flask import Flask, request, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, current_user, logout_user
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import requests
import os
from enum import Enum
from sqlalchemy import func

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///expenses.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Enable CORS for frontend connectivity (allows cross-origin requests from e.g., React/Vue/Angular on different ports)
# For production, restrict origins e.g., origins=["http://localhost:3000"]
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class Role(Enum):
    ADMIN = 'admin'
    MANAGER = 'manager'
    EMPLOYEE = 'employee'

class Status(Enum):
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'

class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    country = db.Column(db.String(100), nullable=False)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.Enum('admin', 'manager', 'employee'), nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    company = db.relationship('Company', backref='users')
    manager = db.relationship('User ', remote_side=[id], backref='direct_subordinates')

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
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
    employee = db.relationship('User ', backref='expenses')

class Approval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expense_id = db.Column(db.Integer, db.ForeignKey('expense.id'), nullable=False)
    approver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    step = db.Column(db.Integer, default=1)
    action = db.Column(db.Enum('approved', 'rejected'))
    comments = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=func.now())
    approver = db.relationship('User ')
    expense = db.relationship('Expense', backref='approvals')

class Workflow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    config = db.Column(db.JSON, default=lambda: {
        'type': 'sequential',
        'steps': [
            {'type': 'manager_of_submitter'},
            {'type': 'role', 'role': 'admin'}
        ],
        'conditional': None  # {'threshold': 60, 'specific': [user_ids]}
    })
    company = db.relationship('Company', backref='workflows')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Decorators
def role_required(*roles):
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Helper functions
def get_currency_for_country(country_name):
    url = f"https://restcountries.com/v3.1/name/{country_name}?fields=name,currencies"
    response = requests.get(url)
    if response.status_code != 200:
        return None
    data = response.json()
    if not data or 'currencies' not in data[0]:
        return None
    currencies = data[0]['currencies']
    return list(currencies.keys())[0] if currencies else None

def convert_currency(amount, from_curr, to_curr):
    if from_curr == to_curr:
        return amount
    url = f"https://api.exchangerate-api.com/v4/latest/{to_curr}"
    response = requests.get(url)
    if response.status_code != 200:
        raise ValueError("Currency conversion failed")
    rates = response.json()['rates']
    if from_curr not in rates:
        raise ValueError("Unsupported currency")
    return amount / rates[from_curr]

def determine_approver(employee, step, company):
    if step['type'] == 'manager_of_submitter':
        return employee.manager
    elif step['type'] == 'role':
        return User.query.filter_by(role=step['role'], company_id=company.id).first()
    elif step['type'] == 'user':
        return User.query.get(step.get('user_id'))
    return None

def create_initial_approvals(expense, workflow_config, employee, company):
    exp_type = workflow_config.get('type', 'sequential')
    steps = workflow_config.get('steps', [])
    if exp_type == 'sequential':
        if steps:
            first_step = steps[0]
            approver = determine_approver(employee, first_step, company)
            if approver:
                approval = Approval(expense_id=expense.id, approver_id=approver.id, step=1)
                db.session.add(approval)
            else:
                expense.status = 'approved'  # No approver, auto-approve
    elif exp_type == 'parallel_conditional':
        for i, step in enumerate(steps, 1):
            approver = determine_approver(employee, step, company)
            if approver:
                approval = Approval(expense_id=expense.id, approver_id=approver.id, step=i)
                db.session.add(approval)
    # For hybrid/combination, extend logic here as needed

def evaluate_conditional(expense_id, workflow_config):
    approvals = Approval.query.filter_by(expense_id=expense_id).all()
    if not approvals:
        return
    approved_count = sum(1 for a in approvals if a.action == 'approved')
    total = len(approvals)
    cond = workflow_config.get('conditional')
    if not cond:
        return
    threshold_met = cond.get('threshold') and (approved_count / total * 100 >= cond['threshold'])
    specific_met = False
    if 'specific' in cond:
        specific_ids = cond['specific']
        specific_met = any(a.action == 'approved' and a.approver_id in specific_ids for a in approvals)
    all_rejected = all(a.action == 'rejected' for a in approvals if a.action is not None)
    if threshold_met or specific_met:
        expense = Expense.query.get(expense_id)
        expense.status = 'approved'
        db.session.commit()
    elif all_rejected:
        expense = Expense.query.get(expense_id)
        expense.status = 'rejected'
        db.session.commit()

# Routes
@app.route('/signup', methods=['POST'])
def signup():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    company_name = data.get('company_name')
    country_name = data.get('country')
    if not all([email, password, company_name, country_name]):
        return jsonify({'error': 'Missing fields'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email exists'}), 400
    currency = get_currency_for_country(country_name)
    if not currency:
        return jsonify({'error': 'Invalid country or no currency'}), 400
    company = Company(name=company_name, currency=currency, country=country_name)
    db.session.add(company)
    db.session.flush()
    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        role='admin',
        company_id=company.id
    )
    db.session.add(user)
    db.session.flush()
    # Default workflow
    default_config = {
        'type': 'sequential',
        'steps': [
            {'type': 'manager_of_submitter'},
            {'type': 'role', 'role': 'admin'}
        ],
        'conditional': None
    }
    workflow = Workflow(company_id=company.id, config=default_config)
    db.session.add(workflow)
    db.session.commit()
    login_user(user)
    return jsonify({'message': 'Signup successful', 'user_id': user.id})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(email=data.get('email')).first()
    if user and check_password_hash(user.password_hash, data.get('password')):
        login_user(user)
        return jsonify({'message': 'Login successful'})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({'message': 'Logged out'})

@app.route('/users', methods=['POST'])
@login_required
@role_required('admin')
def create_user():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    role = data.get('role')
    manager_id = data.get('manager_id')
    company_id = current_user.company_id
    if not all([email, password, role]):
        return jsonify({'error': 'Missing fields'}), 400
    if role not in ['admin', 'manager', 'employee']:
        return jsonify({'error': 'Invalid role'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email exists'}), 400
    if manager_id:
        manager = User.query.get(manager_id)
        if not manager or manager.company_id != company_id:
            return jsonify({'error': 'Invalid manager'}), 400
    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        company_id=company_id,
        manager_id=manager_id
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'id': user.id, 'message': 'User  created'})

@app.route('/users/<int:user_id>', methods=['PUT'])
@login_required
@role_required('admin')
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.company_id != current_user.company_id:
        abort(403)
    data = request.json
    if 'role' in data:
        user.role = data['role']
    if 'manager_id' in data:
        manager = User.query.get(data['manager_id']) if data['manager_id'] else None
        if manager and manager.company_id != current_user.company_id:
            return jsonify({'error': 'Invalid manager'}), 400
        user.manager_id = manager.id if manager else None
    db.session.commit()
    return jsonify({'message': 'User  updated'})

@app.route('/expenses', methods=['POST'])
@login_required
@role_required('employee')
def submit_expense():
    data = request.json
    required = ['amount', 'category', 'description', 'date']
    if not all(field in data for field in required):
        return jsonify({'error': 'Missing fields'}), 400
    try:
        date = datetime.strptime(data['date'], '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date format (YYYY-MM-DD)'}), 400
    company = current_user.company
    currency_original = data.get('currency_original', company.currency)
    try:
        amount_converted = convert_currency(data['amount'], currency_original, company.currency)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    expense = Expense(
        employee_id=current_user.id,
        amount_original=data['amount'],
        currency_original=currency_original,
        amount_converted=amount_converted,
        category=data['category'],
        description=data['description'],
        date=date,
        receipt_url=data.get('receipt_url')
    )
    db.session.add(expense)
    db.session.flush()
    workflow = Workflow.query.filter_by(company_id=company.id).first()
    if workflow:
        create_initial_approvals(expense, workflow.config, current_user, company)
    else:
        expense.status = 'pending'
    db.session.commit()
    return jsonify({'id': expense.id, 'message': 'Expense submitted'})

@app.route('/expenses/my', methods=['GET'])
@login_required
def my_expenses():
    if current_user.role == 'employee':
        expenses = current_user.expenses
    elif current_user.role == 'manager':
        # Direct subordinates only (for multi-level, implement recursive CTE)
        sub_ids = [sub.id for sub in current_user.direct_subordinates]
        expenses = Expense.query.filter(Expense.employee_id.in_(sub_ids)).all()
    else:  # admin
        expenses = Expense.query.join(User).filter(User.company_id == current_user.company_id).all()
    return jsonify([{
        'id': e.id,
        'amount_converted': e.amount_converted,
        'category': e.category,
        'status': e.status.value,
        'date': e.date.isoformat()
    } for e in expenses])

@app.route('/approvals/pending', methods=['GET'])
@login_required
@role_required('manager', 'admin')
def pending_approvals():
    approvals = Approval.query.filter_by(approver_id=current_user.id, action=None).all()
    expense_ids = [a.expense_id for a in approvals]
    expenses = Expense.query.filter(Expense.id.in_(expense_ids)).all()
    # Map approvals to expenses
    exp_dict = {e.id: e for e in expenses}
    result = []
    for approval in approvals:
        exp = exp_dict[approval.expense_id]
        result.append({
            'expense_id': exp.id,
            'approval_id': approval.id,
            'amount_converted': exp.amount_converted,
            'category': exp.category,
            'description': exp.description,
            'step': approval.step,
            'employee_email': exp.employee.email
        })
    return jsonify(result)

@app.route('/approvals/<int:approval_id>/approve', methods=['POST'])
@login_required
@role_required('manager', 'admin')
def approve_expense(approval_id):
    approval = Approval.query.get_or_404(approval_id)
    if approval.approver_id != current_user.id:
        abort(403)
    data = request.json
    approval.action = 'approved'
    approval.comments = data.get('comments', '')
    db.session.commit()
    expense = approval.expense
    workflow = Workflow.query.filter_by(company_id=current_user.company_id).first()
    if workflow:
        config = workflow.config
        exp_type = config.get('type', 'sequential')
        steps = config.get('steps', [])
        if exp_type == 'sequential':
            current_step = approval.step
            if current_step < len(steps):
                next_step_idx = current_step  # 0-based
                next_step = steps[next_step_idx]
                next_approver = determine_approver(expense.employee, next_step, current_user.company)
                if next_approver:
                    next_approval = Approval(
                        expense_id=expense.id,
                        approver_id=next_approver.id,
                        step=current_step + 1
                    )
                    db.session.add(next_approval)
                    db.session.commit()
                    return jsonify({'message': 'Approved, forwarded to next'})
            else:
                expense.status = 'approved'
                db.session.commit()
                return jsonify({'message': 'Approved (final)'})
        elif exp_type == 'parallel_conditional':
            evaluate_conditional(expense.id, config)
            return jsonify({'message': 'Approved, condition evaluated'})
    else:
        expense.status = 'approved'
        db.session.commit()
    return jsonify({'message': 'Approved'})

@app.route('/approvals/<int:approval_id>/reject', methods=['POST'])
@login_required
@role_required('manager', 'admin')
def reject_expense(approval_id):
    approval = Approval.query.get_or_404(approval_id)
    if approval.approver_id != current_user.id:
        abort(403)
    data = request.json
    approval.action = 'rejected'
    approval.comments = data.get('comments', '')
    db.session.commit()
    expense = approval.expense
    workflow = Workflow.query.filter_by(company_id=current_user.company_id).first()
    if workflow and workflow.config.get('type') == 'parallel_conditional':
        evaluate_conditional(expense.id, workflow.config)
    else:
        expense.status = 'rejected'
        expense.comments = approval.comments
        db.session.commit()
    return jsonify({'message': 'Rejected'})

@app.route('/expenses/<int:expense_id>/override', methods=['POST'])
@login_required
@role_required('admin')
def override_approval(expense_id):
    expense = Expense.query.get_or_404(expense_id)
    if expense.employee.company_id != current_user.company_id:
        abort(403)
    data = request.json
    action = data.get('action')  # 'approve' or 'reject'
    comments = data.get('comments', '')
    if action == 'approve':
        expense.status = 'approved'
    elif action == 'reject':
        expense.status = 'rejected'
        expense.comments = comments
    else:
        return jsonify({'error': 'Invalid action'}), 400
    # Cancel pending approvals
    pending_approvals = Approval.query.filter_by(expense_id=expense_id, action=None).all()
    for pa in pending_approvals:
        db.session.delete(pa)
    db.session.commit()
    return jsonify({'message': f'Expense {action}d'})

@app.route('/workflows', methods=['GET', 'PUT'])
@login_required
@role_required('admin')
def manage_workflows():
    company_id = current_user.company_id
    if request.method == 'GET':
        workflow = Workflow.query.filter_by(company_id=company_id).first()
        if workflow:
            return jsonify({'config': workflow.config})
        return jsonify({'error': 'No workflow'}), 404
    elif request.method == 'PUT':
        data = request.json
        workflow = Workflow.query.filter_by(company_id=company_id).first()
        if not workflow:
            return jsonify({'error': 'No workflow found'}), 404
        config = data.get('config')
        if not config or not isinstance(config, dict):
            return jsonify({'error': 'Invalid config'}), 400
        workflow.config = config
        db.session.commit()
        return jsonify({'message': 'Workflow updated', 'config': workflow.config})