# move.os
meh-2050



AI powered app that uses cv and real time post estimations to count repetitions.

## Dependencies

This project uses the following libraries:

- *streamlit* — The main application framework used to build and run the web interface.
- *streamlit-webrtc* — Enables webcam streaming in Streamlit Cloud by allowing the user's browser camera to send video frames to the backend. (cv2.VideoCapture(0) does not work on cloud deployments.)
- *opencv-python* — Used for image processing, frame manipulation, and drawing landmarks.
- *mediapipe* — Provides real-time pose detection for exercise tracking (e.g., squats and push-ups).
- *numpy* — Performs mathematical computations such as angle calculations using vectors and atan2.
- *av* — Required dependency for streamlit-webrtc to handle video frame encoding and decoding.
