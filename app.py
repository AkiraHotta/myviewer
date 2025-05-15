from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import os

load_dotenv()  # ← ここで.env読み込み

app = Flask(__name__)


app.secret_key = 'CHANGE_THIS_TO_A_SECRET_KEY'
# app.secret_key = os.getenv('SECRET_KEY')


# --- データベース設定 ---
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL  # Render用(PostgreSQL)
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///cameras.db'  # ローカル用(SQLite)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- モデル定義 ---
class Camera(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    stream_url = db.Column(db.String(200), nullable=False)

class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag_name = db.Column(db.String(50), nullable=False)

class TagCamera(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag_id = db.Column(db.Integer, db.ForeignKey('tag.id'), nullable=False)
    camera_id = db.Column(db.Integer, db.ForeignKey('camera.id'), nullable=False)

class User(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    role     = db.Column(db.Integer, nullable=False)  # 1=管理者, 2=一般

class TagUser(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    tag_id  = db.Column(db.Integer, db.ForeignKey('tag.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

# --- ログイン必須デコレータ ---
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper

# --- 認証ルート ---
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
        user = User.query.filter_by(username=u).first()
        if user and check_password_hash(user.password, p):
            session['user_id']   = user.id
            session['username']  = user.username
            session['user_role'] = int(user.role)   # ← 明示的に int() で
            return redirect(url_for('index'))
        flash('ログインに失敗しました')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- TOP画面 ---
@app.route('/')
@login_required
def index():
    # adminは「全てのカメラ」タグだけ
    if session['username'] == 'admin':
        all_tag = Tag.query.filter_by(tag_name='全てのカメラ').first()
        user_tag_ids = [all_tag.id] if all_tag else []
    else:
        user_tag_ids = [tu.tag_id for tu in TagUser.query.filter_by(user_id=session['user_id']).all()]

    cameras = (Camera.query
               .join(TagCamera, Camera.id==TagCamera.camera_id)
               .filter(TagCamera.tag_id.in_(user_tag_ids))
               .distinct()
               .all())

    camera_tag_map = { cam.id: [] for cam in cameras }
    for rel in TagCamera.query.filter(TagCamera.tag_id.in_(user_tag_ids)).all():
        if rel.camera_id in camera_tag_map:
            camera_tag_map[rel.camera_id].append(rel.tag_id)

    tags = Tag.query.filter(Tag.id.in_(user_tag_ids)).all()

    return render_template('index.html',
                           cameras=cameras,
                           tags=tags,
                           camera_tag_map=camera_tag_map)




@app.route('/camera', methods=['GET','POST'])
@login_required
def camera():
    if request.method == 'POST':
        name = request.form['camera_name']
        url_ = request.form['stream_url']
        if name and url_:
            camera = Camera(name=name, stream_url=url_)
            db.session.add(camera)
            db.session.commit()

            # 全てのカメラタグ取得
            all_tag = Tag.query.filter_by(tag_name='全てのカメラ').first()
            if all_tag:
                db.session.add(TagCamera(tag_id=all_tag.id, camera_id=camera.id))
                db.session.commit()
    cameras = Camera.query.all()
    return render_template('camera.html', cameras=cameras)

@app.route('/delete_camera/<int:camera_id>')
@login_required
def delete_camera(camera_id):
    c = Camera.query.get(camera_id)
    if c:
        # まずタグカメラ関連も削除
        TagCamera.query.filter_by(camera_id=camera_id).delete()
        db.session.delete(c)
        db.session.commit()
    return redirect(url_for('camera'))

@app.route('/update_camera/<int:camera_id>', methods=['POST'])
@login_required
def update_camera(camera_id):
    data = request.get_json()
    c = Camera.query.get(camera_id)
    if c:
        c.name = data['name']
        c.stream_url = data['stream_url']
        db.session.commit()
    return '', 204

@app.route('/tag', methods=['GET','POST'])
@login_required
def tag():
    cameras = Camera.query.all()
    tags = Tag.query.filter(Tag.tag_name != '全てのカメラ').all()
    if request.method == 'POST':
        tname = request.form['tag_name']
        cam_ids = request.form.get('camera_ids','').split(',')
        if tname and cam_ids:
            tag = Tag.query.filter_by(tag_name=tname).first() or Tag(tag_name=tname)
            db.session.add(tag); db.session.commit()
            for cid in cam_ids:
                db.session.add(TagCamera(tag_id=tag.id, camera_id=int(cid)))
            db.session.commit()
        return redirect(url_for('tag'))
    tag_data = []
    for t in tags:
        cams = Camera.query.join(TagCamera, Camera.id==TagCamera.camera_id)\
                           .filter(TagCamera.tag_id==t.id).all()
        tag_data.append({'tag':t, 'cameras':cams, 'camera_ids':[c.id for c in cams]})
    return render_template('tag.html', cameras=cameras, tag_data=tag_data)

@app.route('/delete_tag/<int:tag_id>')
@login_required
def delete_tag(tag_id):
    TagCamera.query.filter_by(tag_id=tag_id).delete()
    Tag.query.filter_by(id=tag_id).delete()
    db.session.commit()
    return redirect(url_for('tag'))

@app.route('/update_tag/<int:tag_id>', methods=['POST'])
@login_required
def update_tag(tag_id):
    data = request.get_json()
    t = Tag.query.get(tag_id)
    if t:
        t.tag_name = data['tag_name']
        TagCamera.query.filter_by(tag_id=tag_id).delete(); db.session.commit()
        for cid in data.get('camera_ids', []):
            db.session.add(TagCamera(tag_id=tag_id, camera_id=int(cid)))
        db.session.commit()
    return '', 204

# --- アカウント管理ルート ---
@app.route('/account', methods=['GET','POST'])
@login_required
def account():
    print("▶ session:", dict(session))    # ← ここを追加
    if session.get('user_role') != 1:
        return "管理者ではありません", 403

    if request.method == 'POST':
        uname = request.form['username']
        pwd   = request.form['password']
        role  = int(request.form['role'])

        # 重複チェック
        if User.query.filter_by(username=uname).first():
            flash('このアカウント名は既に存在します')
            return redirect(url_for('account'))

        user = User(username=uname,
                    password=generate_password_hash(pwd),
                    role=role)
        db.session.add(user)
        db.session.commit()

        tags = request.form.get('tag_ids','').split(',')
        for tid in tags:
            if tid:
                db.session.add(TagUser(tag_id=int(tid), user_id=user.id))
        db.session.commit()
        return redirect(url_for('account'))

    # --- GETリクエストで必要な情報を返す ---
    users = User.query.all()
    tags  = Tag.query.all()
    user_tag_map = { u.id: [] for u in users }
    for rel in TagUser.query.all():
        user_tag_map[rel.user_id].append(rel.tag_id)
    return render_template('account.html',
                           users=users, tags=tags,
                           user_tag_map=user_tag_map)

@app.route('/delete_user/<int:user_id>')
@login_required
def delete_user(user_id):
    TagUser.query.filter_by(user_id=user_id).delete()
    User.query.filter_by(id=user_id).delete()
    db.session.commit()
    return redirect(url_for('account'))

@app.route('/update_user/<int:user_id>', methods=['POST'])
@login_required
def update_user(user_id):
    print(f"=== /update_user/{user_id} にリクエスト受信 ===")  # ← これ追加
    data = request.get_json()
    print(f"受信データ: {data}")  # ← データ内容も確認

    u = User.query.get(user_id)
    if u:
        # 同じ名前が他のユーザーに存在しないか確認（自分以外）
        existing_user = User.query.filter(User.username == data['username'], User.id != user_id).first()
        if existing_user:
            return 'duplicate', 409  # ←重複エラー

        # 更新処理
        u.username = data['username']
        if data.get('password'):
            u.password = generate_password_hash(data['password'])
        u.role = data['role']
        
        # タグ更新
        TagUser.query.filter_by(user_id=user_id).delete()
        for tid in data.get('tag_ids', []):
            db.session.add(TagUser(tag_id=int(tid), user_id=user_id))
        db.session.commit()
        print(f"=== ユーザー {u.username} を更新しました ===")  # ← 更新確認
    return '', 204


# =============================================================================
# ↓ ここから「起動時に一度だけ」実行される初期化処理です ↓
# =============================================================================

def initialize_db():
    """テーブル作成と admin+全てのカメラタグ生成＋紐付けを行う"""
    db.create_all()

    # adminユーザー
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(username='admin',
                     password=generate_password_hash('1213'),
                     role=1)
        db.session.add(admin)
        db.session.commit()

    # 「全てのカメラ」タグ
    all_tag = Tag.query.filter_by(tag_name='全てのカメラ').first()
    if not all_tag:
        all_tag = Tag(tag_name='全てのカメラ')
        db.session.add(all_tag)
        db.session.commit()

    # admin にタグを紐付け
    if not TagUser.query.filter_by(user_id=admin.id, tag_id=all_tag.id).first():
        TagUser.query.filter_by(user_id=admin.id).delete()
        db.session.add(TagUser(user_id=admin.id, tag_id=all_tag.id))
        db.session.commit()

    # 既存のカメラがあれば同タグに紐付け
    for cam in Camera.query.all():
        if not TagCamera.query.filter_by(tag_id=all_tag.id, camera_id=cam.id).first():
            db.session.add(TagCamera(tag_id=all_tag.id, camera_id=cam.id))
    db.session.commit()

# モジュール読み込み時（＝Gunicorn起動時／ローカルpython実行時）に必ず一度だけ呼び出す
with app.app_context():
    initialize_db()

# =============================================================================
# アプリ起動（ローカル用）
# =============================================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)

    