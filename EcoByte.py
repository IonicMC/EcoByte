import cv2
import numpy as np
import onnxruntime as ort

# Load ONNX model
session = ort.InferenceSession("best.onnx")

# Get input name
input_name = session.get_inputs()[0].name

# Start camera
cap = cv2.VideoCapture(0)

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Resize to model input size
    img = cv2.resize(frame, (640, 640))
    img = img.astype(np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))
    img = np.expand_dims(img, axis=0)

    # Run inference
    outputs = session.run(None, {input_name: img})

    # Just print detections to confirm model works
    print(outputs[0].shape)

    # Show camera feed
    cv2.imshow("YOLOv8 Test", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
