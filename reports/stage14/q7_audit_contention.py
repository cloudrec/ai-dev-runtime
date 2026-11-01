import os, sys, multiprocessing as mp
sys.path.insert(0, "/opt/owneros-autonomy")
DB = sys.argv[1]
os.environ["CONTROL_PLANE_DB"] = DB
def worker(n):
    from core.fullcycle import engine as fe
    conn, _ = fe.connect()
    for i in range(100):
        fe._audit(conn, action="stress", review_id=n, actor=f"w{n}", outcome=str(i))
    conn.close()
if __name__ == "__main__":
    from core.fullcycle import engine as fe
    fe.connect()[0].close()
    ps = [mp.Process(target=worker, args=(n,)) for n in range(4)]
    [p.start() for p in ps]; [p.join() for p in ps]
    print("exitcodes", [p.exitcode for p in ps])
    print(fe.verify_decision_audit())
