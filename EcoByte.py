from ultralytics import YOLO
import cv2

# Load your ONNX model
model = YOLO("best.onnx")

# Open camera
cap = cv2.VideoCapture(0)

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Run detection
    results = model(frame)

    # Draw detections
    annotated_frame = results[0].plot()

    # Show result
    cv2.imshow("YOLOv8 Test", annotated_frame)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
