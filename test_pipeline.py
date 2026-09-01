import json, struct, math, threading, urllib.request

HOST = "http://localhost:8000"

def wav(seconds=0.2):
    sr, n = 16000, int(16000 * seconds)
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / sr))) for i in range(n))
    return b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm

def transcribe(audio, language="en"):
    boundary = "lt"
    body = (b"--lt\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.wav\"\r\nContent-Type: audio/wav\r\n\r\n" + audio + b"\r\n--lt\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n" + language.encode() + b"\r\n--lt--\r\n")
    req = urllib.request.Request(HOST + "/api/transcribe", data=body, headers={"Content-Type": "multipart/form-data; boundary=lt"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))

def post_json(path, value):
    req = urllib.request.Request(HOST + path, data=json.dumps(value).encode(), headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))

assert json.load(urllib.request.urlopen(HOST + "/api/self-check"))["ready"]
assert post_json("/api/sessions", {"title": "Contract", "language": "fi"})["title"] == "Contract"
assert all(marker in urllib.request.urlopen(HOST + "/").read().decode() for marker in ["识别：芬兰语", "识别：英语", "保存录音+原文", "AI整理（保留原文）"])
results = []
threads = [threading.Thread(target=lambda: results.append(transcribe(wav()))) for _ in range(2)]
for thread in threads: thread.start()
for thread in threads: thread.join()
assert len(results) == 2 and all("audio_duration" in result for result in results)
assert post_json("/api/translate", {"text": "Our next lecture covers chapter five."})["text"]
assert isinstance(post_json("/api/terms", {"text": "Photosynthesis converts light energy."})["terms"], list)
print("lecture translator contract: passed")
