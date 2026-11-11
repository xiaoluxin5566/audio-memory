import json

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))

def save_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(".partial")
    temp.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    temp.replace(path)
