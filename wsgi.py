from app import create_app
from app.db import get_db, init_db
from app.stats import precompute_tables
from app.engine import ROUND1_SIZE, GROUP_SIZE

app = create_app()

with app.app_context():
    conn = get_db()
    init_db(conn)
    conn.close()
    # 预热 Fisher 缓存：首轮 20/20、第二轮 40/40 全部列联表
    precompute_tables([
        (ROUND1_SIZE, ROUND1_SIZE),
        (GROUP_SIZE, GROUP_SIZE),
    ])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
