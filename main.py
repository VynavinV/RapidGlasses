import cv2

STREAM_URL = "http://10.94.64.101:81/stream"


def main():
    cap = cv2.VideoCapture(STREAM_URL)

    if not cap.isOpened():
        print(f"Failed to open stream: {STREAM_URL}")
        return

    print("Streaming... press ESC or 'q' to quit.")

    while True:
        ret, frame = cap.read()

        if not ret:
            print("Failed to read frame from stream.")
            break

        cv2.imshow("ESP32 Camera", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == 27 or key == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
