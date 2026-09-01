FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static ./static

# ctranslate2 4.4 uses cuDNN 8 compatibility names; expose the matching
# cuDNN 9 libraries shipped by this CUDA image under those names.
RUN cd /usr/lib/x86_64-linux-gnu \
    && for f in libcudnn libcudnn_ops libcudnn_adv libcudnn_cnn libcudnn_engines_precompiled libcudnn_engines_runtime_compiled libcudnn_heuristic libcudnn_graph; do \
         if [ -e ${f}.so.9 ]; then ln -sf ${f}.so.9 ${f}.so.8; fi; \
       done \
    && ln -sf libcudnn_ops.so.9 libcudnn_ops_infer.so.8 \
    && ln -sf libcudnn_cnn.so.9 libcudnn_cnn_infer.so.8 \
    && ln -sf libcudnn_adv.so.9 libcudnn_adv_infer.so.8 \
    && ldconfig

EXPOSE 8000
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
