# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, make_response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from functools import wraps
import os
import hashlib
import re
import logging
from decimal import Decimal, ROUND_HALF_UP

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ============ CONFIGURATION ============
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///loan_saving.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'False').lower() == 'true'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Fix for Render PostgreSQL URL (Render uses postgres://, SQLAlchemy needs postgresql://)
if app.config['SQLALCHEMY_DATABASE_URI'] and app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

logger.info(f"Database URI: {app.config['SQLALCHEMY_DATABASE_URI'][:50] if app.config['SQLALCHEMY_DATABASE_URI'] else 'None'}...")

db = SQLAlchemy(app)

# ============ HELPER FUNCTIONS ============
def validate_nigerian_phone(phone):
    """Validate Nigerian phone numbers"""
    pattern = re.compile(r'^(0|234)?[789][01]\d{8}$')
    return bool(pattern.match(phone))

def sanitize_input(text):
    """Sanitize user input"""
    if not text:
        return text
    import html
    return html.escape(text.strip())

# ============ DATABASE MODELS ============

class Organization(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=True)
    subscription_plan = db.Column(db.String(50), default='basic')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    max_customers = db.Column(db.Integer, default=500)
    max_storage_mb = db.Column(db.Integer, default=100)
    business_logo = db.Column(db.String(200))
    business_address = db.Column(db.String(300))
    business_phone = db.Column(db.String(20))
    
    # Relationships
    admin = db.relationship('Admin', backref='organization', foreign_keys=[admin_id])
    
    def can_add_customer(self):
        current_count = Customer.query.filter_by(org_id=self.id).count()
        return current_count < self.max_customers
    
    def get_stats(self):
        customers = Customer.query.filter_by(org_id=self.id, is_active=True).all()
        return {
            'customer_count': len(customers),
            'active_loans': Loan.query.filter_by(org_id=self.id, status='active').count(),
            'total_savings': sum(c.total_savings() for c in customers),
            'total_loans_outstanding': sum(c.total_loan_balance() for c in customers)
        }

class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120))
    full_name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    is_master_admin = db.Column(db.Boolean, default=False)
    organization_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=True)
    
    def set_password(self, password):
        """Hash password using SHA-256"""
        self.password_hash = hashlib.sha256(password.encode()).hexdigest()
    
    def check_password(self, password):
        """Verify password"""
        return self.password_hash == hashlib.sha256(password.encode()).hexdigest()
    
    def get_org_filtered_query(self, model):
        """Get query filtered by organization"""
        if self.organization_id:
            return model.query.filter_by(org_id=self.organization_id)
        return model.query

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(100))
    address = db.Column(db.String(200))
    registration_date = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    # Make phone unique per organization
    __table_args__ = (db.UniqueConstraint('org_id', 'phone', name='unique_phone_per_org'),)
    
    # Relationships
    savings = db.relationship('Saving', backref='customer', lazy=True, cascade='all, delete-orphan')
    loans = db.relationship('Loan', backref='customer', lazy=True, cascade='all, delete-orphan')
    proxy_collections = db.relationship('ProxyCollection', backref='customer', lazy=True)
    org = db.relationship('Organization', backref='customers')
    
    def total_savings(self):
        total = sum(s.amount for s in self.savings if s.transaction_type == 'deposit')
        total -= sum(s.amount for s in self.savings if s.transaction_type == 'withdrawal')
        return total
    
    def total_loan_balance(self):
        total_borrowed = sum(l.amount for l in self.loans if l.status == 'active')
        total_repaid = sum(l.amount_repaid for l in self.loans if l.status == 'active')
        return total_borrowed - total_repaid

class Saving(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)
    description = db.Column(db.String(200))
    transaction_date = db.Column(db.DateTime, default=datetime.utcnow)
    
    org = db.relationship('Organization', backref='savings')

class Loan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    amount_repaid = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='active')
    loan_date = db.Column(db.DateTime, default=datetime.utcnow)
    repayment_due_date = db.Column(db.DateTime)
    description = db.Column(db.String(200))
    
    org = db.relationship('Organization', backref='loans')
    
    def remaining_balance(self):
        return self.amount - self.amount_repaid

class ProxyCollection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=False)
    collector_name = db.Column(db.String(100), nullable=False)
    collector_phone = db.Column(db.String(20))
    collection_date = db.Column(db.DateTime, default=datetime.utcnow)
    collection_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reference_id = db.Column(db.Integer)
    relationship = db.Column(db.String(100))
    
    org = db.relationship('Organization', backref='proxy_collections')

class LoanPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False)
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(50))
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    proxy_collection_id = db.Column(db.Integer, db.ForeignKey('proxy_collection.id'))
    
    loan = db.relationship('Loan', backref='payments')
    proxy_collection = db.relationship('ProxyCollection', backref='loan_payment')
    org = db.relationship('Organization', backref='loan_payments')

class PasswordResetToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=False)
    token = db.Column(db.String(100), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    
    admin = db.relationship('Admin', backref='reset_tokens')

# ============ DECORATORS ============

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        
        # Verify admin still exists and is active
        admin = Admin.query.get(session['admin_id'])
        if not admin or not admin.is_active:
            session.clear()
            flash('Your account has been deactivated', 'danger')
            return redirect(url_for('login'))
        
        # Verify organization is active
        if admin.organization_id:
            org = Organization.query.get(admin.organization_id)
            if org and not org.is_active:
                session.clear()
                flash('Your organization has been deactivated. Please contact support.', 'danger')
                return redirect(url_for('login'))
        
        return f(*args, **kwargs)
    return decorated_function

def master_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        admin = Admin.query.get(session['admin_id'])
        if not admin or not admin.is_master_admin:
            flash('Master admin access required', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def org_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        admin = Admin.query.get(session['admin_id'])
        if not admin.organization_id and not admin.is_master_admin:
            flash('Organization admin access required', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# ============ TEMPLATE FILTERS ============

@app.template_filter('format_currency')
def format_currency(value):
    """Format currency with commas and Naira symbol"""
    if value is None:
        return "₦0.00"
    try:
        formatted = f"{value:,.2f}"
        return f"₦{formatted}"
    except (ValueError, TypeError):
        return f"₦{value}"

@app.template_filter('format_number')
def format_number(value):
    """Format number with commas (no decimal places)"""
    if value is None:
        return "0"
    try:
        return f"{value:,.0f}"
    except (ValueError, TypeError):
        return str(value)

@app.template_filter('format_decimal')
def format_decimal(value):
    """Format decimal with commas (no currency symbol)"""
    if value is None:
        return "0.00"
    try:
        return f"{value:,.2f}"
    except (ValueError, TypeError):
        return str(value)

# ============ CONTEXT PROCESSOR ============

@app.context_processor
def utility_processor():
    def get_current_admin():
        if 'admin_id' in session:
            return Admin.query.get(session['admin_id'])
        return None
    
    def get_current_org():
        admin = get_current_admin()
        if admin and admin.organization_id:
            return Organization.query.get(admin.organization_id)
        return None
    
    def is_master_admin():
        admin = get_current_admin()
        return admin and admin.is_master_admin
    
    return dict(
        get_current_admin=get_current_admin,
        get_current_org=get_current_org,
        is_master_admin=is_master_admin
    )

# ============ CORRECTED DATABASE INITIALIZATION ============

def init_database():
    """Initialize database with default data - CORRECTED ORDER for PostgreSQL"""
    with app.app_context():
        # Create all tables first
        db.create_all()
        logger.info("Database tables created/verified")
        
        # Check if master admin exists (by looking for is_master_admin=True)
        master_admin = Admin.query.filter_by(is_master_admin=True).first()
        
        if not master_admin:
            logger.info("Creating master admin and organization...")
            
            # STEP 1: Create admin FIRST (without organization_id)
            master_admin = Admin(
                username='master_admin',
                full_name='Master Administrator',
                email='master@loansaving.com',
                is_master_admin=True,
                is_active=True,
                organization_id=None  # No organization yet
            )
            master_admin.set_password('MasterAdmin123!')
            db.session.add(master_admin)
            db.session.flush()  # This gives master_admin a REAL ID (1, not 0)
            
            print(f"✅ Admin created with ID: {master_admin.id}")
            logger.info(f"Admin created with ID: {master_admin.id}")
            
            # STEP 2: Create organization with the REAL admin_id
            master_org = Organization(
                name="Master Admin Organization",
                admin_id=master_admin.id,  # Use the REAL ID, NOT 0!
                max_customers=999999,
                subscription_plan="enterprise",
                is_active=True
            )
            db.session.add(master_org)
            db.session.flush()
            
            print(f"✅ Organization created with admin_id: {master_org.admin_id}")
            logger.info(f"Organization created with admin_id: {master_org.admin_id}")
            
            # STEP 3: Update admin with organization_id
            master_admin.organization_id = master_org.id
            
            db.session.commit()
            
            print("=" * 60)
            print("✅ MASTER ADMIN CREATED SUCCESSFULLY!")
            print("📋 Username: master_admin")
            print("🔑 Password: MasterAdmin123!")
            print("=" * 60)
            logger.info("Master admin created successfully")
        else:
            logger.info("Master admin already exists")
        
        # Check if demo organization exists
        demo_org = Organization.query.filter_by(name="Demo Business").first()
        if not demo_org:
            logger.info("Creating demo organization...")
            
            # STEP 1: Create demo admin FIRST
            demo_admin = Admin(
                username='demo_admin',
                full_name='Demo Administrator',
                email='demo@example.com',
                is_master_admin=False,
                is_active=True,
                organization_id=None
            )
            demo_admin.set_password('demo123')
            db.session.add(demo_admin)
            db.session.flush()
            
            print(f"✅ Demo admin created with ID: {demo_admin.id}")
            logger.info(f"Demo admin created with ID: {demo_admin.id}")
            
            # STEP 2: Create demo organization with REAL admin_id
            demo_org = Organization(
                name="Demo Business",
                admin_id=demo_admin.id,  # Use REAL ID, NOT 0!
                max_customers=50,
                subscription_plan="basic",
                business_address="123 Demo Street, Lagos, Nigeria",
                business_phone="08012345678",
                is_active=True
            )
            db.session.add(demo_org)
            db.session.flush()
            
            # STEP 3: Update demo admin with organization_id
            demo_admin.organization_id = demo_org.id
            
            db.session.commit()
            
            print("✅ Demo organization created!")
            print("📋 Demo Admin: demo_admin / demo123")
            logger.info("Demo organization created successfully")
        else:
            logger.info("Demo organization already exists")

# Run initialization
init_database()

# ============ AUTHENTICATION ROUTES ============

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'admin_id' in session:
        admin = Admin.query.get(session['admin_id'])
        if admin and admin.is_master_admin:
            return redirect(url_for('master_dashboard'))
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        logger.info(f"Login attempt - Username: {username}")
        
        admin = Admin.query.filter_by(username=username).first()
        
        if admin and admin.check_password(password):
            if not admin.is_active:
                flash('Your account has been deactivated', 'danger')
                return redirect(url_for('login'))
            
            # Check if organization is active
            if admin.organization_id:
                org = Organization.query.get(admin.organization_id)
                if org and not org.is_active:
                    flash('Your organization has been deactivated', 'danger')
                    return redirect(url_for('login'))
            
            admin.last_login = datetime.utcnow()
            db.session.commit()
            
            session['admin_id'] = admin.id
            session['admin_username'] = admin.username
            session['admin_name'] = admin.full_name
            session['is_master_admin'] = admin.is_master_admin
            
            flash(f'Welcome back, {admin.full_name or admin.username}!', 'success')
            
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            
            if admin.is_master_admin:
                return redirect(url_for('master_dashboard'))
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out successfully', 'info')
    return redirect(url_for('login'))

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    admin = Admin.query.get(session['admin_id'])
    
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if not admin.check_password(current_password):
            flash('Current password is incorrect', 'danger')
            return redirect(url_for('change_password'))
        
        if new_password != confirm_password:
            flash('New passwords do not match', 'danger')
            return redirect(url_for('change_password'))
        
        if len(new_password) < 6:
            flash('Password must be at least 6 characters long', 'danger')
            return redirect(url_for('change_password'))
        
        admin.set_password(new_password)
        db.session.commit()
        
        flash('Password changed successfully!', 'success')
        return redirect(url_for('index'))
    
    return render_template('change_password.html', admin=admin)

# ============ MASTER ADMIN ROUTES ============

@app.route('/')
@login_required
def index():
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        return redirect(url_for('master_dashboard'))
    
    if not admin.organization_id:
        flash('No organization assigned. Please contact master admin.', 'danger')
        return redirect(url_for('logout'))
    
    org = Organization.query.get(admin.organization_id)
    stats = org.get_stats()
    
    # Get recent customers
    recent_customers = Customer.query.filter_by(org_id=org.id, is_active=True).order_by(Customer.registration_date.desc()).limit(5).all()
    
    # Get recent transactions
    recent_savings = Saving.query.filter_by(org_id=org.id).order_by(Saving.transaction_date.desc()).limit(5).all()
    
    return render_template('dashboard.html', 
                         org=org,
                         stats=stats,
                         recent_customers=recent_customers,
                         recent_savings=recent_savings)

@app.route('/master/dashboard')
@master_admin_required
def master_dashboard():
    organizations = Organization.query.all()
    total_organizations = len(organizations)
    total_customers = sum(Customer.query.filter_by(org_id=org.id).count() for org in organizations)
    total_active_loans = sum(Loan.query.filter_by(org_id=org.id, status='active').count() for org in organizations)
    total_savings = 0
    total_loans_outstanding = 0
    for org in organizations:
        stats = org.get_stats()
        total_savings += stats['total_savings']
        total_loans_outstanding += stats['total_loans_outstanding']
    
    # Recent organizations
    recent_orgs = organizations[:5]
    
    return render_template('master/dashboard.html',
                         total_organizations=total_organizations,
                         total_customers=total_customers,
                         total_active_loans=total_active_loans,
                         total_savings=total_savings,
                         total_loans_outstanding=total_loans_outstanding,
                         recent_orgs=recent_orgs)

@app.route('/master/organizations')
@master_admin_required
def manage_organizations():
    organizations = Organization.query.all()
    
    org_stats = []
    for org in organizations:
        stats = org.get_stats()
        stats['org'] = org
        org_stats.append(stats)
    
    return render_template('master/organizations.html', org_stats=org_stats)

@app.route('/master/create-organization', methods=['GET', 'POST'])
@master_admin_required
def create_organization():
    if request.method == 'POST':
        org_name = request.form.get('org_name')
        admin_username = request.form.get('admin_username')
        admin_password = request.form.get('admin_password')
        admin_email = request.form.get('admin_email')
        admin_full_name = request.form.get('admin_full_name')
        business_phone = request.form.get('business_phone')
        business_address = request.form.get('business_address')
        max_customers = int(request.form.get('max_customers', 500))
        subscription_plan = request.form.get('subscription_plan', 'basic')
        
        # Validate
        if Admin.query.filter_by(username=admin_username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('create_organization'))
        
        if len(admin_password) < 6:
            flash('Password must be at least 6 characters', 'danger')
            return redirect(url_for('create_organization'))
        
        # STEP 1: Create admin FIRST (without organization)
        new_admin = Admin(
            username=admin_username,
            email=admin_email,
            full_name=admin_full_name,
            is_master_admin=False,
            is_active=True,
            organization_id=None
        )
        new_admin.set_password(admin_password)
        db.session.add(new_admin)
        db.session.flush()
        
        # STEP 2: Create organization with the REAL admin_id
        organization = Organization(
            name=org_name,
            admin_id=new_admin.id,  # Use the REAL ID, NOT 0!
            max_customers=max_customers,
            subscription_plan=subscription_plan,
            business_phone=business_phone,
            business_address=business_address,
            is_active=True
        )
        db.session.add(organization)
        db.session.flush()
        
        # STEP 3: Update admin with organization_id
        new_admin.organization_id = organization.id
        
        db.session.commit()
        
        flash(f'Organization "{org_name}" created successfully! Admin: {admin_username}', 'success')
        return redirect(url_for('manage_organizations'))
    
    return render_template('master/create_organization.html')

@app.route('/master/organization/<int:org_id>/edit', methods=['GET', 'POST'])
@master_admin_required
def edit_organization(org_id):
    org = Organization.query.get_or_404(org_id)
    
    if request.method == 'POST':
        org.name = request.form.get('org_name')
        org.max_customers = int(request.form.get('max_customers', 500))
        org.subscription_plan = request.form.get('subscription_plan', 'basic')
        org.business_phone = request.form.get('business_phone')
        org.business_address = request.form.get('business_address')
        
        db.session.commit()
        flash(f'Organization "{org.name}" updated successfully!', 'success')
        return redirect(url_for('manage_organizations'))
    
    return render_template('master/edit_organization.html', org=org)

@app.route('/master/organization/<int:org_id>/toggle-status')
@master_admin_required
def toggle_org_status(org_id):
    org = Organization.query.get_or_404(org_id)
    org.is_active = not org.is_active
    db.session.commit()
    status = "activated" if org.is_active else "deactivated"
    flash(f'Organization "{org.name}" has been {status}', 'success')
    return redirect(url_for('manage_organizations'))

@app.route('/master/organization/<int:org_id>/delete', methods=['POST'])
@master_admin_required
def delete_organization(org_id):
    org = Organization.query.get_or_404(org_id)
    
    # Don't delete master org
    if org.name == "Master Admin Organization":
        flash('Cannot delete the master organization', 'danger')
        return redirect(url_for('manage_organizations'))
    
    org_name = org.name
    db.session.delete(org)
    db.session.commit()
    
    flash(f'Organization "{org_name}" has been permanently deleted', 'warning')
    return redirect(url_for('manage_organizations'))

@app.route('/master/login-as/<int:admin_id>')
@master_admin_required
def login_as_admin(admin_id):
    admin = Admin.query.get_or_404(admin_id)
    
    if not admin.organization:
        flash('This admin does not belong to an organization', 'danger')
        return redirect(url_for('manage_organizations'))
    
    # Store original master admin ID to allow switching back
    session['original_admin_id'] = session.get('admin_id')
    session['original_is_master'] = True
    session['admin_id'] = admin.id
    session['admin_username'] = admin.username
    session['admin_name'] = admin.full_name
    session['is_master_admin'] = False
    
    flash(f'Now logged in as {admin.username} from {admin.organization.name}', 'info')
    return redirect(url_for('index'))

@app.route('/master/switch-back')
@login_required
def switch_back():
    if 'original_admin_id' in session:
        original_id = session.pop('original_admin_id')
        session.pop('original_is_master', None)
        original_admin = Admin.query.get(original_id)
        if original_admin and original_admin.is_master_admin:
            session['admin_id'] = original_admin.id
            session['admin_username'] = original_admin.username
            session['admin_name'] = original_admin.full_name
            session['is_master_admin'] = True
            flash('Switched back to master admin', 'info')
            return redirect(url_for('master_dashboard'))
    
    flash('Unable to switch back', 'warning')
    return redirect(url_for('index'))

@app.route('/master/admins')
@master_admin_required
def manage_all_admins():
    admins = Admin.query.filter_by(is_master_admin=False).all()
    return render_template('master/admins.html', admins=admins)

@app.route('/master/admin/<int:admin_id>/toggle-status')
@master_admin_required
def toggle_admin_status(admin_id):
    admin = Admin.query.get_or_404(admin_id)
    if admin.is_master_admin:
        flash('Cannot modify master admin', 'danger')
    else:
        admin.is_active = not admin.is_active
        db.session.commit()
        status = "activated" if admin.is_active else "deactivated"
        flash(f'Admin {admin.username} has been {status}', 'success')
    return redirect(url_for('manage_all_admins'))

# ============ CUSTOMER MANAGEMENT ROUTES ============

@app.route('/customers')
@login_required
def customers():
    admin = Admin.query.get(session['admin_id'])
    if admin.is_master_admin:
        search = request.args.get('search', '')
        query = Customer.query
        if search:
            query = query.filter(
                (Customer.name.contains(search)) | 
                (Customer.phone.contains(search))
            )
        customers_list = query.all()
    else:
        org_id = admin.organization_id
        search = request.args.get('search', '')
        query = Customer.query.filter_by(org_id=org_id, is_active=True)
        if search:
            query = query.filter(
                (Customer.name.contains(search)) | 
                (Customer.phone.contains(search))
            )
        customers_list = query.all()
    
    return render_template('customers.html', customers=customers_list, search=search)

@app.route('/customers/deleted')
@login_required
def deleted_customers():
    admin = Admin.query.get(session['admin_id'])
    if admin.is_master_admin:
        customers_list = Customer.query.filter_by(is_active=False).all()
    else:
        customers_list = Customer.query.filter_by(org_id=admin.organization_id, is_active=False).all()
    
    return render_template('deleted_customers.html', customers=customers_list)

@app.route('/customer/add', methods=['GET', 'POST'])
@login_required
def add_customer():
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        flash('Master admin cannot add customers directly. Please login as an organization admin.', 'warning')
        return redirect(url_for('master_dashboard'))
    
    org_id = admin.organization_id
    org = Organization.query.get(org_id)
    
    if request.method == 'POST':
        name = sanitize_input(request.form.get('name'))
        phone = sanitize_input(request.form.get('phone'))
        email = sanitize_input(request.form.get('email'))
        address = sanitize_input(request.form.get('address'))
        
        # Validate phone
        if not validate_nigerian_phone(phone):
            flash('Invalid Nigerian phone number format. Use format: 080XXXXXXXX', 'danger')
            return redirect(url_for('add_customer'))
        
        # Check if customer already exists in this organization
        existing = Customer.query.filter_by(org_id=org_id, phone=phone).first()
        if existing:
            flash('Customer with this phone number already exists in your organization!', 'danger')
            return redirect(url_for('add_customer'))
        
        # Check organization limits
        if not org.can_add_customer():
            flash(f'You have reached the maximum customer limit of {org.max_customers}. Please upgrade your plan.', 'danger')
            return redirect(url_for('customers'))
        
        customer = Customer(
            name=name,
            phone=phone,
            email=email,
            address=address,
            org_id=org_id
        )
        
        db.session.add(customer)
        db.session.commit()
        
        flash('Customer registered successfully!', 'success')
        return redirect(url_for('customers'))
    
    return render_template('add_customer.html')

@app.route('/customer/<int:customer_id>')
@login_required
def view_customer(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
    
    savings_history = customer.savings
    loans = Loan.query.filter_by(customer_id=customer_id, status='active').all()
    completed_loans = Loan.query.filter_by(customer_id=customer_id, status='completed').all()
    
    return render_template('view_customer.html', 
                         customer=customer, 
                         savings_history=savings_history,
                         loans=loans,
                         completed_loans=completed_loans)

@app.route('/customer/<int:customer_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_customer(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
    
    if request.method == 'POST':
        customer.name = sanitize_input(request.form.get('name'))
        customer.phone = sanitize_input(request.form.get('phone'))
        customer.email = sanitize_input(request.form.get('email'))
        customer.address = sanitize_input(request.form.get('address'))
        
        db.session.commit()
        flash('Customer updated successfully!', 'success')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    return render_template('edit_customer.html', customer=customer)

@app.route('/customer/<int:customer_id>/delete', methods=['POST'])
@login_required
def delete_customer(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
    
    # Check if customer has any active loans
    active_loans = Loan.query.filter_by(customer_id=customer_id, status='active').all()
    if active_loans:
        flash(f'Cannot delete {customer.name} because they have active loans. Please settle all loans first.', 'danger')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    customer.is_active = False
    db.session.commit()
    
    flash(f'Customer {customer.name} has been deactivated successfully!', 'success')
    return redirect(url_for('customers'))

@app.route('/customer/<int:customer_id>/restore', methods=['POST'])
@login_required
def restore_customer(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
    
    customer.is_active = True
    db.session.commit()
    
    flash(f'Customer {customer.name} has been restored successfully!', 'success')
    return redirect(url_for('customers'))

@app.route('/customer/<int:customer_id>/permanent_delete', methods=['POST'])
@login_required
def permanent_delete_customer(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    # Only master admin or org admin can permanently delete
    if not admin.is_master_admin and not admin.organization_id:
        flash('Permission denied', 'danger')
        return redirect(url_for('customers'))
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
    
    active_loans = Loan.query.filter_by(customer_id=customer_id, status='active').all()
    if active_loans:
        flash(f'Cannot permanently delete {customer.name} because they have active loans.', 'danger')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    customer_name = customer.name
    db.session.delete(customer)
    db.session.commit()
    
    flash(f'Customer {customer_name} has been permanently deleted from the system!', 'warning')
    return redirect(url_for('customers'))

# ============ SAVINGS AND LOAN ROUTES ============

@app.route('/customer/<int:customer_id>/add_saving', methods=['POST'])
@login_required
def add_saving(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
        org_id = customer.org_id
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
        org_id = admin.organization_id
    
    amount = float(request.form.get('amount'))
    description = sanitize_input(request.form.get('description', ''))
    transaction_type = request.form.get('transaction_type')
    
    if amount <= 0:
        flash('Amount must be greater than zero', 'danger')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    if transaction_type == 'withdrawal' and amount > customer.total_savings():
        flash('Insufficient savings balance!', 'danger')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    saving = Saving(
        customer_id=customer_id,
        org_id=org_id,
        amount=amount,
        transaction_type=transaction_type,
        description=description
    )
    
    db.session.add(saving)
    db.session.commit()
    
    flash(f'Saving {transaction_type} of ₦{amount:,.2f} recorded successfully!', 'success')
    return redirect(url_for('view_customer', customer_id=customer_id))

@app.route('/customer/<int:customer_id>/add_loan', methods=['POST'])
@login_required
def add_loan(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
        org_id = customer.org_id
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
        org_id = admin.organization_id
    
    amount = float(request.form.get('amount'))
    description = sanitize_input(request.form.get('description', ''))
    
    if amount <= 0:
        flash('Loan amount must be greater than zero', 'danger')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    loan = Loan(
        customer_id=customer_id,
        org_id=org_id,
        amount=amount,
        description=description
    )
    
    db.session.add(loan)
    db.session.commit()
    
    flash(f'Loan of ₦{amount:,.2f} disbursed successfully!', 'success')
    return redirect(url_for('view_customer', customer_id=customer_id))

@app.route('/customer/<int:customer_id>/repay_loan', methods=['POST'])
@login_required
def repay_loan(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
        org_id = customer.org_id
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
        org_id = admin.organization_id
    
    loan_id = int(request.form.get('loan_id'))
    amount = float(request.form.get('amount'))
    payment_method = request.form.get('payment_method')
    
    loan = Loan.query.get_or_404(loan_id)
    
    if amount <= 0:
        flash('Payment amount must be greater than zero', 'danger')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    if amount > loan.remaining_balance():
        flash(f'Payment amount cannot exceed remaining balance of {loan.remaining_balance()}', 'danger')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    if payment_method == 'savings_deduction':
        if amount > customer.total_savings():
            flash('Insufficient savings to cover this payment!', 'danger')
            return redirect(url_for('view_customer', customer_id=customer_id))
        
        saving = Saving(
            customer_id=customer_id,
            org_id=org_id,
            amount=amount,
            transaction_type='withdrawal',
            description=f'Loan payment deduction for loan #{loan_id}'
        )
        db.session.add(saving)
    
    # Record loan payment
    payment = LoanPayment(
        loan_id=loan_id,
        org_id=org_id,
        amount=amount,
        payment_method=payment_method
    )
    db.session.add(payment)
    
    # Update loan repayment amount
    loan.amount_repaid += amount
    
    # Check if loan is fully repaid
    if loan.amount_repaid >= loan.amount:
        loan.status = 'completed'
        flash('Loan fully repaid! Congratulations!', 'success')
    
    db.session.commit()
    
    flash(f'Loan payment of ₦{amount:,.2f} recorded successfully!', 'success')
    return redirect(url_for('view_customer', customer_id=customer_id))

@app.route('/customer/<int:customer_id>/proxy_collection', methods=['GET', 'POST'])
@login_required
def proxy_collection(customer_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customer = Customer.query.get_or_404(customer_id)
        org_id = customer.org_id
    else:
        customer = Customer.query.filter_by(id=customer_id, org_id=admin.organization_id).first_or_404()
        org_id = admin.organization_id
    
    if request.method == 'POST':
        collector_name = sanitize_input(request.form.get('collector_name'))
        collector_phone = sanitize_input(request.form.get('collector_phone'))
        collection_type = request.form.get('collection_type')
        amount = float(request.form.get('amount'))
        relationship = sanitize_input(request.form.get('relationship'))
        loan_id = request.form.get('loan_id') if collection_type == 'loan' else None
        
        if amount <= 0:
            flash('Amount must be greater than zero', 'danger')
            return redirect(url_for('view_customer', customer_id=customer_id))
        
        proxy = ProxyCollection(
            customer_id=customer_id,
            org_id=org_id,
            collector_name=collector_name,
            collector_phone=collector_phone,
            collection_type=collection_type,
            amount=amount,
            relationship=relationship
        )
        
        db.session.add(proxy)
        
        if collection_type == 'loan' and loan_id:
            loan = Loan.query.get(loan_id)
            if loan:
                if amount > loan.remaining_balance():
                    flash(f'Payment amount cannot exceed remaining balance of {loan.remaining_balance()}', 'danger')
                    return redirect(url_for('view_customer', customer_id=customer_id))
                
                payment = LoanPayment(
                    loan_id=loan_id,
                    org_id=org_id,
                    amount=amount,
                    payment_method='proxy',
                    proxy_collection_id=proxy.id
                )
                db.session.add(payment)
                loan.amount_repaid += amount
                
                if loan.amount_repaid >= loan.amount:
                    loan.status = 'completed'
        elif collection_type == 'saving':
            saving = Saving(
                customer_id=customer_id,
                org_id=org_id,
                amount=amount,
                transaction_type='deposit',
                description=f'Proxy collection by {collector_name}'
            )
            db.session.add(saving)
        
        db.session.commit()
        
        flash(f'Proxy collection recorded successfully! Amount: ₦{amount:,.2f}', 'success')
        return redirect(url_for('view_customer', customer_id=customer_id))
    
    active_loans = Loan.query.filter_by(customer_id=customer_id, status='active').all()
    return render_template('proxy_collection.html', customer=customer, active_loans=active_loans)

# ============ HISTORY ROUTES ============

@app.route('/proxy_history')
@login_required
def proxy_history():
    admin = Admin.query.get(session['admin_id'])
    
    search_name = request.args.get('search_name', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    collection_type = request.args.get('collection_type', '')
    
    if admin.is_master_admin:
        query = ProxyCollection.query
    else:
        query = ProxyCollection.query.filter_by(org_id=admin.organization_id)
    
    if search_name:
        query = query.filter(
            (ProxyCollection.collector_name.contains(search_name)) |
            (ProxyCollection.customer.has(Customer.name.contains(search_name))) |
            (ProxyCollection.customer.has(Customer.phone.contains(search_name)))
        )
    
    if start_date:
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
        query = query.filter(ProxyCollection.collection_date >= start_date_obj)
    
    if end_date:
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d')
        end_date_obj = end_date_obj.replace(hour=23, minute=59, second=59)
        query = query.filter(ProxyCollection.collection_date <= end_date_obj)
    
    if collection_type:
        query = query.filter_by(collection_type=collection_type)
    
    proxy_collections = query.order_by(ProxyCollection.collection_date.desc()).all()
    
    total_amount = sum(p.amount for p in proxy_collections)
    loan_collections = sum(p.amount for p in proxy_collections if p.collection_type == 'loan')
    saving_collections = sum(p.amount for p in proxy_collections if p.collection_type == 'saving')
    
    return render_template('proxy_history.html',
                         proxy_collections=proxy_collections,
                         search_name=search_name,
                         start_date=start_date,
                         end_date=end_date,
                         collection_type=collection_type,
                         total_amount=total_amount,
                         loan_collections=loan_collections,
                         saving_collections=saving_collections)

@app.route('/savings_history')
@login_required
def savings_history():
    admin = Admin.query.get(session['admin_id'])
    
    search_name = request.args.get('search_name', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    transaction_type = request.args.get('transaction_type', '')
    
    if admin.is_master_admin:
        query = Saving.query
    else:
        query = Saving.query.filter_by(org_id=admin.organization_id)
    
    if search_name:
        query = query.filter(Saving.customer.has(Customer.name.contains(search_name)))
    
    if start_date:
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
        query = query.filter(Saving.transaction_date >= start_date_obj)
    
    if end_date:
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d')
        end_date_obj = end_date_obj.replace(hour=23, minute=59, second=59)
        query = query.filter(Saving.transaction_date <= end_date_obj)
    
    if transaction_type:
        query = query.filter_by(transaction_type=transaction_type)
    
    savings = query.order_by(Saving.transaction_date.desc()).all()
    
    total_deposits = sum(s.amount for s in savings if s.transaction_type == 'deposit')
    total_withdrawals = sum(s.amount for s in savings if s.transaction_type == 'withdrawal')
    net_savings = total_deposits - total_withdrawals
    
    return render_template('savings_history.html',
                         savings=savings,
                         search_name=search_name,
                         start_date=start_date,
                         end_date=end_date,
                         transaction_type=transaction_type,
                         total_deposits=total_deposits,
                         total_withdrawals=total_withdrawals,
                         net_savings=net_savings)

@app.route('/proxy_collection/<int:proxy_id>')
@login_required
def view_proxy_details(proxy_id):
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        proxy = ProxyCollection.query.get_or_404(proxy_id)
    else:
        proxy = ProxyCollection.query.filter_by(id=proxy_id, org_id=admin.organization_id).first_or_404()
    
    return render_template('proxy_details.html', proxy=proxy)

@app.route('/reports')
@login_required
def reports():
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        total_customers = Customer.query.filter_by(is_active=True).count()
        total_savings = sum(c.total_savings() for c in Customer.query.filter_by(is_active=True).all())
        total_loans_outstanding = sum(c.total_loan_balance() for c in Customer.query.filter_by(is_active=True).all())
        active_loans = Loan.query.filter_by(status='active').count()
    else:
        org_id = admin.organization_id
        customers = Customer.query.filter_by(org_id=org_id, is_active=True).all()
        total_customers = len(customers)
        total_savings = sum(c.total_savings() for c in customers)
        total_loans_outstanding = sum(c.total_loan_balance() for c in customers)
        active_loans = Loan.query.filter_by(org_id=org_id, status='active').count()
    
    return render_template('reports.html',
                         total_customers=total_customers,
                         total_savings=total_savings,
                         total_loans_outstanding=total_loans_outstanding,
                         active_loans=active_loans)

# ============ ORGANIZATION PROFILE ============

@app.route('/organization/profile', methods=['GET', 'POST'])
@login_required
def org_profile():
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        flash('Master admin does not have an organization profile', 'warning')
        return redirect(url_for('master_dashboard'))
    
    org = Organization.query.get(admin.organization_id)
    stats = org.get_stats()
    
    if request.method == 'POST':
        org.business_phone = request.form.get('business_phone')
        org.business_address = request.form.get('business_address')
        # Update admin info
        admin.full_name = request.form.get('full_name')
        admin.email = request.form.get('email')
        
        db.session.commit()
        session['admin_name'] = admin.full_name
        flash('Organization profile updated successfully!', 'success')
        return redirect(url_for('org_profile'))
    
    return render_template('org_profile.html', org=org, admin=admin, stats=stats)

# ============ EXPORT ROUTES ============

@app.route('/export/customers/csv')
@login_required
def export_customers_csv():
    admin = Admin.query.get(session['admin_id'])
    
    if admin.is_master_admin:
        customers = Customer.query.filter_by(is_active=True).all()
    else:
        customers = Customer.query.filter_by(org_id=admin.organization_id, is_active=True).all()
    
    import csv
    from io import StringIO
    
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Phone', 'Email', 'Address', 'Registration Date', 'Total Savings', 'Loan Balance'])
    
    for customer in customers:
        writer.writerow([
            customer.name,
            customer.phone,
            customer.email or '',
            customer.address or '',
            customer.registration_date.strftime('%Y-%m-%d'),
            f"₦{customer.total_savings():,.2f}",
            f"₦{customer.total_loan_balance():,.2f}"
        ])
    
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=customers_export.csv'
    return response

@app.route('/export/transactions/csv')
@login_required
def export_transactions_csv():
    admin = Admin.query.get(session['admin_id'])
    
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    if admin.is_master_admin:
        query = Saving.query
    else:
        query = Saving.query.filter_by(org_id=admin.organization_id)
    
    if start_date:
        query = query.filter(Saving.transaction_date >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        query = query.filter(Saving.transaction_date <= datetime.strptime(end_date, '%Y-%m-%d'))
    
    transactions = query.all()
    
    import csv
    from io import StringIO
    
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Customer', 'Type', 'Amount', 'Description'])
    
    for t in transactions:
        writer.writerow([
            t.transaction_date.strftime('%Y-%m-%d %H:%M:%S'),
            t.customer.name,
            t.transaction_type,
            f"₦{t.amount:,.2f}",
            t.description or ''
        ])
    
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=transactions_export.csv'
    return response

# ============ HEALTH CHECK ROUTE (for Render) ============

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.utcnow().isoformat()}), 200

# ============ RUN APP ============

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    print("\n" + "=" * 60)
    print("🚀 LOAN & SAVINGS MANAGER")
    print("=" * 60)
    print("📋 LOGIN CREDENTIALS:")
    print("   Master Admin: master_admin / MasterAdmin123!")
    print("   Demo Admin: demo_admin / demo123")
    print("=" * 60)
    print(f"🌐 Running on port: {port}")
    print(f"🐛 Debug mode: {debug_mode}")
    print("=" * 60 + "\n")
    
    app.run(debug=debug_mode, host='0.0.0.0', port=port)
