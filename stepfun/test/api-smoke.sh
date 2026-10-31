#!/usr/bin/env bash
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:7860}
curl -fsS "$BASE/api/health"
python3 - <<'PY'
import json, os, requests
base=os.environ.get('BASE','http://127.0.0.1:7860')
v=[0.0]*384
docs=[{'title':'OceanBase','text':'OceanBase 是分布式数据库，支持向量与全文检索。','embedding':v},
      {'title':'SeekDB','text':'SeekDB 支持本地嵌入式部署与混合检索。','embedding':v}]
r=requests.post(base+'/api/docs',json={'documents':docs},timeout=20); r.raise_for_status(); print(r.text)
r=requests.post(base+'/api/search',json={'query':'数据库 检索','embedding':v,'limit':5},timeout=20); r.raise_for_status(); print(r.text)
PY
