import cv2
import numpy as np

MODEL_PATH = "best.onnx"
IMG_SIZE = 640

net = cv2.dnn.readNetFromONNX(MODEL_PATH)

cap = cv2.VideoCapture(0)

while True:
    ret, frame = cap.read()
    if not ret:
        break

    blob = cv2.dnn.blobFromImage(
        frame,
        scalefactor=1/255.0,
        size=(IMG_SIZE, IMG_SIZE),
        swapRB=True,
        crop=False
    )

    net.setInput(blob)
    outputs = net.forward()

    arr = np.squeeze(outputs)

    print("Max value:", np.max(arr))

    cv2.imshow("EcoByte Camera", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
