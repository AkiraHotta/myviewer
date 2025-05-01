# people_counter.py

import os, sys
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.append(project_root)

# ローカル用実行中の __main__ から直接 current_tracked_set を持ってくる
#import __main__ as main_mod
#current_tracked_set = main_mod.current_tracked_set
#tracked_set_lock     = main_mod.tracked_set_lock


#render用 app.py 側で管理している current_tracked_set / tracked_set_lock を直接インポート
from app import current_tracked_set, tracked_set_lock

import argparse, time, datetime, math
import cv2
import numpy as np
from ultralytics import YOLO






# ── フレーム毎の位置・ステータスキャッシュ ──────────────────────
# run_counter が書き込む最新トラッキング情報を SSE で配信するため
last_positions = {}

# ---------- CentroidTracker --------------------- #
class CentroidTracker:
    def __init__(self, max_disappear=30):
        self.next_id = 1
        self.objects = {}     # id -> (x,y)
        self.miss    = {}     # id -> frame count
        self.max_disappear = max_disappear

    def update(self, rects):
        if len(rects) == 0:
            for oid in list(self.objects.keys()):
                self.miss[oid] += 1
                if self.miss[oid] > self.max_disappear:
                    self._deregister(oid)
            return self.objects

        centroids = np.array([((x1+x2)//2, (y1+y2)//2) for (x1,y1,x2,y2) in rects])
        if not self.objects:
            for c in centroids:
                self._register(c)
            return self.objects

        obj_ids = list(self.objects.keys())
        obj_pts = np.array(list(self.objects.values()), dtype=float)
        D = np.linalg.norm(
            np.expand_dims(obj_pts,1) - np.expand_dims(centroids,0), axis=2
        )
        rows = D.min(axis=1).argsort()
        cols = D.argmin(axis=1)[rows]
        used_r, used_c = set(), set()
        for r, c in zip(rows, cols):
            if r in used_r or c in used_c:
                continue
            oid = obj_ids[r]
            self.objects[oid] = tuple(centroids[c])
            self.miss[oid] = 0
            used_r.add(r); used_c.add(c)
        for idx in range(len(centroids)):
            if idx not in used_c:
                self._register(tuple(centroids[idx]))
        for r in range(len(obj_pts)):
            if r not in used_r:
                oid = obj_ids[r]
                self.miss[oid] += 1
                if self.miss[oid] > self.max_disappear:
                    self._deregister(oid)
        return self.objects

    def _register(self, cen):
        self.objects[self.next_id] = cen
        self.miss[self.next_id] = 0
        self.next_id += 1

    def _deregister(self, oid):
        # 画面外消失時の OUT を記録したい場合はここで log_event を呼ぶ
        self.objects.pop(oid, None)
        self.miss.pop(oid, None)
# ------------------------------------------------ #

def log_event(cam_id: int, direction: str):
    from app import app, db, VisitorEvent
    now = datetime.datetime.utcnow()
    with app.app_context():
        db.session.add(VisitorEvent(camera_id=cam_id,
                                    direction=direction,
                                    date=now.date(),
                                    time=now.time()))
        db.session.commit()

def run_counter(cam_id: int, url: str, line_ratio: float, show: bool = False):
    

    from app import stream_ok
    # 起動時は「OK」としておく
    stream_ok[cam_id] = True
    print(f"[run_counter START] cam_id={cam_id}, initial tracked_set={current_tracked_set}")

   



    from app import app
    from app import CameraLine  # モデルをループ内で使うため事前にimport


    # ウィンドウ表示設定
    if show:
        cv2.namedWindow(f"Cam{cam_id}", cv2.WINDOW_NORMAL)

    # ストリームオープン
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        print(f"[Cam{cam_id}] Cannot open stream: {url}")
        
        # ストリームオープンに失敗 → 異常とマーク
        stream_ok[cam_id] = False
        return
    
    

    # モデルとトラッカー初期化
    
    from ultralytics import YOLO
    import os

    # app.py で読み込んだ設定を参照
    from app import YOLO_MODEL_TYPE

    # モデルファイル名を環境変数設定に合わせて決定
    model_path = f"models/{YOLO_MODEL_TYPE}.pt"
    model = YOLO(model_path)




    print("[YOLO] model loaded.")
    tracker = CentroidTracker()
    prev    = {}  # id -> (side, y)
    status  = {}  # id -> 'in'/'out'
    frame_count = 0

    while True:
        # 追跡OFFならスリープしてスキップ
        with tracked_set_lock:
            if cam_id not in current_tracked_set:
                # ← ここにも追加
                print(f"[run_counter] cam_id={cam_id} is NOT in tracked_set → sleeping")
                time.sleep(3)
                continue
             # ← ここにも追加
        print(f"[run_counter] cam_id={cam_id} found in tracked_set → resuming")
    # ── ここまで追加 ──────────────────────────────
        ok, frame = cap.read()
        frame_count += 1
       
        if not ok:
            # フレーム取得失敗 → 異常とマーク
            stream_ok[cam_id] = False
            time.sleep(3)
            cap.open(url)
            continue

        # フレーム取得成功 → OK に戻す
        if not stream_ok.get(cam_id):
            stream_ok[cam_id] = True

        # ── 動的に境界線情報を再取得 ──────────────────────
        with app.app_context():
            line_rec = CameraLine.query.get(cam_id)
        if not line_rec:
            print(f"[Cam{cam_id}] no CameraLine set, skipping.")
            time.sleep(5)
            continue

        # 新しい境界線パラメータを毎フレーム反映
        lx1, ly1, lx2, ly2 = map(float, (line_rec.x1, line_rec.y1, line_rec.x2, line_rec.y2))
        in_side = line_rec.in_side

        H, W = frame.shape[:2]
        px1, py1 = int(lx1 * W), int(ly1 * H)
        px2, py2 = int(lx2 * W), int(ly2 * H)

        # 境界線描画
        cv2.line(frame, (px1, py1), (px2, py2), (0, 255, 0), 2)
        cv2.putText(frame, f"Line:({px1},{py1})->({px2},{py2})",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)

        # 推論 & トラッキング
        results = model(frame, imgsz=640, conf=0.4, classes=[0], verbose=False)
        rects   = [tuple(map(int, xyxy)) for r in results for *xyxy, conf, cls in r.boxes.data.tolist()]
        objs    = tracker.update(rects)

        # クロッシング判定
        for oid, (cx, cy) in objs.items():
            cross = (px2 - px1)*(cy - py1) - (py2 - py1)*(cx - px1)
            side_now  = 1 if cross > 0 else -1
            side_prev = prev.get(oid, (side_now, cy))[0]

            if side_now != side_prev:
                entering = (side_now < 0 and in_side=='A') or (side_now>0 and in_side=='B')
                direction = 'in' if entering else 'out'
                print(f"[Cam{cam_id}] Frame{frame_count} Obj{oid}: crossing -> {direction.upper()}")
                log_event(cam_id, direction)
                status[oid] = direction
            prev[oid] = (side_now, cy)

        # キャッシュに正規化座標とステータスを保存
        last_positions[cam_id] = [
            {'id': oid,
             'x': cx / W,
             'y': cy / H,
             'status': status.get(oid)}
            for oid,(cx,cy) in objs.items()
        ]

        # ウィンドウ表示
        if show:
            cv2.imshow(f"Cam{cam_id}", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    cap.release()
    if show:
        cv2.destroyWindow(f"Cam{cam_id}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera-id", type=int, required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--line-ratio", type=float, default=0.5,
                    help="水平ライン比率 (カスタム線使用時は無視)")
    ap.add_argument("--show", action="store_true", help="Show video with overlay")
    args = ap.parse_args()
    run_counter(args.camera_id, args.url, args.line_ratio, show=args.show)