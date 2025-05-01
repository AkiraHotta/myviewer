from gevent import monkey
monkey.patch_all()

# ────────── ここから既存の import ──────────

from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import os
from datetime import datetime
from sqlalchemy import func, case
import datetime
from flask import jsonify
import time, json
from flask import Response, stream_with_context
import threading
import threading
from threading import Lock
from dotenv import load_dotenv



load_dotenv()  # ← ここで.env読み込み

YOLO_MODEL_TYPE = os.getenv("YOLO_MODEL_TYPE", "yolov8n") 

app = Flask(__name__)

app.config["YOLO_MODEL_TYPE"] = YOLO_MODEL_TYPE

# ── ここから追加 ──────────────────────────────
# 現在追跡中のカメラIDを管理するセット（スレッドセーフにするためLock付き）
current_tracked_set = set()
tracked_set_lock = Lock()
# ── ここまで追加 ──────────────────────────────
# ── ここから追加 ──────────────────────────────
# メモリ上に最新のラインパラメータを保持する dict
# key: camera_id, value: (x1, y1, x2, y2, in_side)
current_line_params = {}
# 各カメラのストリーム接続正常かを記録する dict (True=OK, False=異常)
stream_ok = {}
# ── ここまで追加 ──────────────────────────────


#app.secret_key = 'CHANGE_THIS_TO_A_SECRET_KEY'

# 環境変数から読み込む
app.secret_key = os.getenv('SECRET_KEY')

# --- データベース設定 ---
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL  # Render用(PostgreSQL)
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///cameras.db'  # ローカル用(SQLite)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- モデル定義（この 1 回だけにする） ---
class Camera(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    stream_url = db.Column(db.String(200), nullable=False)
    enabled    = db.Column(db.Boolean, nullable=False, default=True)
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

class CameraLine(db.Model):
    # camera_id を主キーにして 1 対 1
    camera_id = db.Column(db.Integer, db.ForeignKey('camera.id'), primary_key=True)
    x1        = db.Column(db.Float,   nullable=False)  # 0.0–1.0 正規化済み
    y1        = db.Column(db.Float,   nullable=False)
    x2        = db.Column(db.Float,   nullable=False)
    y2        = db.Column(db.Float,   nullable=False)
    in_side   = db.Column(db.String(1), nullable=False) # 'A' or 'B'

class VisitorEvent(db.Model):                # ★ 追加したいのはこれだけ
    id         = db.Column(db.Integer, primary_key=True)
    camera_id  = db.Column(db.Integer, db.ForeignKey('camera.id'), nullable=False)
    direction  = db.Column(db.String(3), nullable=False)   # 'in' / 'out'
    date       = db.Column(db.Date,  nullable=False)
    time       = db.Column(db.Time,  nullable=False)

    __table_args__ = (
        db.Index('idx_event_cam_date', 'camera_id', 'date'),
    )



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

@app.route('/traffic')
@login_required
def traffic():
    today = datetime.date.today()    # 今日 1 日分の集計
    subq = (db.session.query(
                VisitorEvent.camera_id,
                func.sum(case(
                    (VisitorEvent.direction=='in',  1),
                    (VisitorEvent.direction=='out',-1),
                    else_=0)).label('stay'),
                func.sum(case(
                    (VisitorEvent.direction=='in',1),
                    else_=0)).label('total_in'))
            .filter(VisitorEvent.date == today)
            .group_by(VisitorEvent.camera_id)
            .subquery())

    rows = (db.session.query(Camera,
                             subq.c.total_in,
                             subq.c.stay)
            .outerjoin(subq, Camera.id == subq.c.camera_id)
            .all())

    data = []
    for cam, total_in, stay in rows:
        total_in = total_in or 0
        stay     = stay or 0

        # ← ここで退場者数を計算
        total_out = total_in - stay

        status   = '空き' if stay<=0 else ('通常' if stay<=30 else '混雑')
        data.append({
            'camera':    cam,
            'total_in':  total_in,
            'total_out': total_out,   # ← ここを追加
            'stay':      stay,
            'status':    status
        })
    return render_template('traffic.html', data=data)

# ← ここから下に追加 ─────────────────────────────

@app.route('/set_line/<int:camera_id>', methods=['POST'])
@login_required
def set_line(camera_id):
    data = request.get_json()
    cl = CameraLine.query.get(camera_id)
    if not cl:
        cl = CameraLine(
            camera_id = camera_id,
            x1         = data['x1'],
            y1         = data['y1'],
            x2         = data['x2'],
            y2         = data['y2'],
            in_side    = data['in_side']
        )
        db.session.add(cl)
    else:
        cl.x1      = data['x1']
        cl.y1      = data['y1']
        cl.x2      = data['x2']
        cl.y2      = data['y2']
        cl.in_side = data['in_side']
    db.session.commit()
     # ── ここから追加 ─────────────────────────
    # メモリキャッシュを更新
    current_line_params[camera_id] = (
        float(data['x1']), float(data['y1']),
        float(data['x2']), float(data['y2']),
        data['in_side']
    )
    # ── ここまで追加 ─────────────────────────
    return '', 204

@app.route('/clear_line/<int:camera_id>', methods=['POST'])
@login_required
def clear_line(camera_id):
    CameraLine.query.filter_by(camera_id=camera_id).delete()
    db.session.commit()

    # ── ここから追加 ─────────────────────────
    # メモリキャッシュから削除
    current_line_params.pop(camera_id, None)
    # ── ここまで追加 ─────────────────────────
    return '', 204

# 既存の set_line / clear_line のすぐ下に追加してください
@app.route('/get_line/<int:camera_id>', methods=['GET'])
@login_required
def get_line(camera_id):
    cl = CameraLine.query.get(camera_id)
    if not cl:
        return jsonify({}), 404
    # 正規化済み座標をそのまま返す
    return jsonify({
        'x1':      cl.x1,
        'y1':      cl.y1,
        'x2':      cl.x2,
        'y2':      cl.y2,
        'in_side': cl.in_side
    }), 200

@app.route('/stream_counts/<int:camera_id>')
@login_required
def stream_counts(camera_id):
    # ① last_positions はループ外で一度だけインポート
    from people_counter import last_positions

    @stream_with_context
    def generate():
        while True:
            today = datetime.date.today()

            # ② カウントは必ず取得
            in_count = VisitorEvent.query.filter_by(
                camera_id=camera_id,
                direction='in',
                date=today
            ).count() or 0
            out_count = VisitorEvent.query.filter_by(
                camera_id=camera_id,
                direction='out',
                date=today
            ).count() or 0

            # ③ last_positions[camera_id] はリストなので、.items() ではなく for で回す
            pts = []
            try:
                for p in last_positions.get(camera_id, []):
                    pts.append({
                        'id':     p['id'],
                        'x':      p['x'],
                        'y':      p['y'],
                        'status': p.get('status')   # 'in' / 'out' / None
                    })
            except Exception as e:
                # もし何かおかしくても、in/out は必ず送る
                pts = []

            # ④ 最終的に in/out と pts を一緒に送信
            payload = {'in': in_count, 'out': out_count, 'pts': pts}
            yield f"data: {json.dumps(payload)}\n\n"

            time.sleep(3)

    return Response(generate(), mimetype='text/event-stream')

@app.route('/stream_table')
@login_required
def stream_table():
    def generate():
        while True:
            today = datetime.date.today()

            # 各カメラの in/out/stay/status を集計
            rows = db.session.query(
                Camera.id,
                func.sum(
       case(
           (VisitorEvent.direction == 'in', 1),
           else_=0
       )
   ).label('total_in'),
   func.sum(
       case(
           (VisitorEvent.direction == 'out', 1),
           else_=0
       )
   ).label('total_out')
            ).outerjoin(VisitorEvent, 
                (VisitorEvent.camera_id==Camera.id)&(VisitorEvent.date==today)
            ).group_by(Camera.id).all()

            data = []
            for cam_id, total_in, total_out in rows:
                total_in  = total_in  or 0
                total_out = total_out or 0
                stay      = total_in - total_out
                status    = '空き' if stay<=0 else ('通常' if stay<=30 else '混雑')
                
                from app import stream_ok
                data.append({
                    'id':         cam_id,
                    'total_in':   total_in,
                    'total_out':  total_out,
                    'stay':       stay,
                    'status':     status,
                    'stream_ok':  bool(stream_ok.get(cam_id, False))
                })

            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            time.sleep(3)

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream')



@app.route('/update_tracked', methods=['POST'])
@login_required
def update_tracked():
    # POSTで送られたカメラIDリストを取得
    ids = set(map(int, request.form.getlist('tracked_cameras')))
    # DB更新
    for cam in Camera.query.all():
        cam.enabled    = (cam.id in ids)
    db.session.commit()
    # グローバルセットも同期
    with tracked_set_lock:
        current_tracked_set.clear()
        current_tracked_set.update(ids)
    return redirect(url_for('traffic'))

@app.before_request
def _start_counters_on_request():
    start_counters()




# ── initialize_db() 以下はそのまま ─────────────────


# =============================================================================
# ↓ ここから「起動時に一度だけ」実行される初期化処理です ↓
# =============================================================================

def initialize_db():
    """テーブル作成と admin+全てのカメラタグ生成＋紐付けを行う"""
    db.create_all()

    # ── adminユーザー生成 ─────────────────────

    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(username='admin',
                     password=generate_password_hash('1213'),
                     role=1)
        db.session.add(admin)
        db.session.commit()

    # ── 「全てのカメラ」タグ生成 ───────────────
    all_tag = Tag.query.filter_by(tag_name='全てのカメラ').first()
    if not all_tag:
        all_tag = Tag(tag_name='全てのカメラ')
        db.session.add(all_tag)
        db.session.commit()

    # ── admin に「全てのカメラ」タグを紐付け ────
    if not TagUser.query.filter_by(user_id=admin.id, tag_id=all_tag.id).first():
        TagUser.query.filter_by(user_id=admin.id).delete()
        db.session.add(TagUser(user_id=admin.id, tag_id=all_tag.id))
        db.session.commit()

    # ── ★ここから追加：デフォルトカメラのシード──（⭐︎テスト環境追加）

    #


    default_cameras = [
            ("MainLabo 1Ｆ RCC", "https://hls.myyou.jp/hls/stream2.m3u8"),
            ("MainLabo 3Ｆ TpLink", "https://hls.myyou.jp/hls/stream1.m3u8"),
            ("MainLabo 3Ｆ RCC",   "https://hls.myyou.jp/hls/stream3.m3u8"),
            ("MainLabo 2Ｆ RCC",   "https://hls.myyou.jp/hls/stream4.m3u8"),
            ("MainLabo 2Ｆ2 RCC",  "https://hls.myyou.jp/hls/stream5.m3u8")
        ]
    for name, url in default_cameras:
            cam = Camera.query.filter_by(stream_url=url).first()
            if not cam:
                cam = Camera(name=name, stream_url=url, enabled=True)
                db.session.add(cam)
                db.session.commit()  # cam.id を確定させる
            # その後に全てのカメラタグを紐付け
            if not TagCamera.query.filter_by(tag_id=all_tag.id, camera_id=cam.id).first():
                db.session.add(TagCamera(tag_id=all_tag.id, camera_id=cam.id))
                db.session.commit()

    #── デフォルト境界線を一括シード ─────────────────
    for cam in Camera.query.all():
            if not CameraLine.query.get(cam.id):
                cl = CameraLine(
                    camera_id=cam.id,
                    x1=0.0, y1=0.5,
                    x2=1.0, y2=0.5,
                    in_side='A'
                )
                db.session.add(cl)
    db.session.commit()

                
    # ── ★ここまで追加────────────────────────


    # 既存のカメラがあれば同タグに紐付け
    for cam in Camera.query.all():
        if not TagCamera.query.filter_by(tag_id=all_tag.id, camera_id=cam.id).first():
            db.session.add(TagCamera(tag_id=all_tag.id, camera_id=cam.id))
    db.session.commit()
# ── ↑ ここまで initialize_db() ──────────────────

# ── ここから追加 ────────────────────────────────────
counters_started = False

def start_counters():
    global counters_started
    if counters_started:
        return
    counters_started = True

    # DB初期化（テーブル作成＋admin/タグ）
    initialize_db()

    # CameraLine をキャッシュ
    for cl in CameraLine.query.all():
        current_line_params[cl.camera_id] = (
            cl.x1, cl.y1, cl.x2, cl.y2, cl.in_side
        )

   # すべてのカメラでスレッドを立てる
    cams = Camera.query.all()

    with tracked_set_lock:
        # ただし current_tracked_set には enabled=True のカメラだけを登録
        current_tracked_set.clear()
        current_tracked_set.update(cam.id for cam in cams if cam.enabled)
    # run_counter スレッド起動
    line_ratio = float(os.getenv('LINE_RATIO', 0.5))
    from people_counter import run_counter
    for cam in cams:
        threading.Thread(
            target=run_counter,
            args=(cam.id, cam.stream_url, line_ratio),
            daemon=True
        ).start()
# ── ここまで追加 ────────────────────────────────────




# =============================================================================
# アプリ起動（ローカル用）
# =============================================================================
if __name__ == '__main__':
    # ローカル実行時も start_counters() で初期化＋スレッド起動
    with app.app_context():
        start_counters()
    app.run(host='0.0.0.0', port=8000)

    