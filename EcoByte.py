import cv2
import numpy as np

MODEL_PATH = "best.onnx"
IMG_SIZE = 640
CONF_THRESHOLD = 0.5

print("Loading ONNX model...")
net = cv2.dnn.readNetFromONNX(MODEL_PATH)
print("Model loaded")

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Camera failed to open")
    exit()

print("Camera opened")

while True:

    ret, frame = cap.read()
    if not ret:
        break

    h, w = frame.shape[:2]

    blob = cv2.dnn.blobFromImage(
        frame,
        scalefactor=1/255.0,
        size=(IMG_SIZE, IMG_SIZE),
        swapRB=True,
        crop=False
    )

    net.setInput(blob)
    outputs = net.forward()

    outputs = np.squeeze(outputs)

    if outputs.ndim == 2:
        outputs = outputs.T

    boxes = []
    scores = []

    for row in outputs:

        if len(row) < 6:
            continue

        x, y, bw, bh = row[:4]
        conf = row[4]

        if conf < CONF_THRESHOLD:
            continue

        left = int((x - bw/2) * w / IMG_SIZE)
        top = int((y - bh/2) * h / IMG_SIZE)
        width = int(bw * w / IMG_SIZE)
        height = int(bh * h / IMG_SIZE)

        boxes.append([left, top, width, height])
        scores.append(float(conf))

    indices = cv2.dnn.NMSBoxes(boxes, scores, CONF_THRESHOLD, 0.4)

    for i in indices:
        i = i[0] if isinstance(i, (tuple, list, np.ndarray)) else i
        x, y, bw, bh = boxes[i]

        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0,255,0), 2)
        cv2.putText(frame, "Bottle", (x, y-10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)

    cv2.imshow("EcoByte Bottle Detector", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
