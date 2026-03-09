import cv2
import numpy as np

print("Loading ONNX model...")

net = cv2.dnn.readNetFromONNX("best.onnx")

print("Model loaded successfully!")

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Camera failed to open")
    exit()

print("Camera opened")

while True:
    ret, frame = cap.read()

    if not ret:
        print("Camera read failed")
        break

    blob = cv2.dnn.blobFromImage(
        frame,
        scalefactor=1/255.0,
        size=(640,640),
        swapRB=True,
        crop=False
    )

    net.setInput(blob)
    outputs = net.forward()

    print("Inference ran")

    cv2.imshow("EcoByte Camera Test", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
