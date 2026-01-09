#!/usr/bin/env python3
import cv2
import time
import sys
import os
import argparse
import threading
import queue
import subprocess 
from datetime import datetime

class AsyncVideoWriter:
    """ Background thread for I/O (Producer-Consumer Pattern) """
    def __init__(self, filename, fourcc, fps, frame_size, queue_size=128):
        self.filename = filename
        self.fourcc = fourcc
        self.fps = fps
        self.frame_size = frame_size
        self.queue = queue.Queue(maxsize=queue_size)
        self.writer = None
        self.thread = None
        self.is_recording = False
        self.dropped_frames = 0

    def start(self):
        try:
            print(f"  -> [IO] Initializing VideoWriter: {self.filename}")
            print(f"  -> [IO] Codec Settings: {self.frame_size} @ {self.fps:.2f} FPS")
            self.writer = cv2.VideoWriter(
                self.filename, self.fourcc, self.fps, self.frame_size
            )
            if not self.writer.isOpened():
                print(f"[Error] Failed to open Writer.")
                self.writer = None
                return False
        except Exception as e:
            print(f"[Error] Writer exception: {e}")
            return False

        self.is_recording = True
        self.dropped_frames = 0
        self.thread = threading.Thread(target=self._process_queue, daemon=True)
        self.thread.start()
        return True

    def write(self, frame):
        if not self.is_recording: return
        try:
            # Non-blocking put
            self.queue.put_nowait(frame)
        except queue.Full:
            self.dropped_frames += 1

    def stop(self):
        """Safely stops the thread and flushes the buffer"""
        self.is_recording = False
        if self.thread:
            # Signal the consumer thread to exit
            self.queue.put(None)
            self.thread.join() # Wait for writing to finish
            self.thread = None
        
        if self.writer:
            self.writer.release()
            self.writer = None
        
        # Calculate file size
        size_mb = 0
        if os.path.exists(self.filename):
            size_mb = os.path.getsize(self.filename) / (1024 * 1024)
            
        print(f"[Rec] Video Saved: {self.filename}")
        print(f"      Size: {size_mb:.2f} MB | Dropped Frames: {self.dropped_frames}")

    def _process_queue(self):
        while True:
            frame = self.queue.get()
            if frame is None: # Sentinel detected
                self.queue.task_done()
                break
            
            if self.writer:
                self.writer.write(frame)
            self.queue.task_done()

class CameraApp:
    def __init__(self):
        self.parse_args()
        self.is_running = True
        self.async_recorder = None
        
        # Statistics
        self.frame_count = 0
        self.start_time = 0
        self.current_fps = 0.0
        
        # Hardware Config Flag (The Latch)
        self.hw_configured = False  
        
        # Paths
        self.output_dir = "captures"
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        self.init_camera()

    def parse_args(self):
        parser = argparse.ArgumentParser(description="Arducam v7 Production", add_help=False)
        # Device settings
        parser.add_argument("-d", "--device", type=int, default=0, help="Video Device Index")
        parser.add_argument("-w", "--width", type=int, default=1920, help="Resolution Width")
        parser.add_argument("-h", "--height", type=int, default=1080, help="Resolution Height")
        parser.add_argument("-f", "--fps", type=int, default=30, help="Target FPS")
        parser.add_argument("--help", action="help", help="Show this help")
        self.args = parser.parse_args()

    def apply_stream_active_controls(self):
        """
        Executes v4l2-ctl commands ONCE after stream starts.
        Updated: Removed exposure_auto_priority.
        """
        dev_node = f"/dev/video{self.args.device}"
        
        # Only setting Frame Rate as requested
        cmds = [
            f"frame_rate={self.args.fps}"
        ]

        print(f"\n[Ctrl] Stream active. Forcing V4L2 controls on {dev_node}...")
        for c in cmds:
            full_cmd = ["v4l2-ctl", "-d", dev_node, "-c", c]
            try:
                subprocess.run(full_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"  -> Applied: {c}")
            except Exception:
                print(f"  -> Failed: {c} (Driver might not support it)")
        print("[Ctrl] Camera Configured.\n")

    def init_camera(self):
        print(f"[Init] Connecting to Camera {self.args.device}...")
        
        backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(self.args.device, backend)
        
        if not self.cap.isOpened():
            print(f"[Fatal] Failed to open /dev/video{self.args.device}")
            sys.exit(1)

        # Basic Setup
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.args.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.args.height)
        
        # Retrieve actual negotiated resolution
        self.actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[Init] Canvas Size: {self.actual_w}x{self.actual_h}")
        print(f"[Init] Waiting for video stream to lock controls...")

    def generate_filename(self, ext):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.output_dir, f"cam_{ts}.{ext}")

    def get_smart_fps(self):
        # If camera is running fine, use real fps. If starting up (low), use target.
        if self.current_fps > 5.0:
            return self.current_fps
        return float(self.args.fps)

    def toggle_recording(self):
        # STOP RECORDING
        if self.async_recorder and self.async_recorder.is_recording:
            print("\n[User] Request: Stop Recording...")
            self.async_recorder.stop()
            self.async_recorder = None
        
        # START RECORDING
        else:
            print("\n[User] Request: Start Recording...")
            filename = self.generate_filename("mp4")
            # Try mp4v for compression, fallback to MJPG if needed
            fourcc = cv2.VideoWriter_fourcc(*'mp4v') 
            
            # Sync header FPS with reality
            rec_fps = self.get_smart_fps()
            
            self.async_recorder = AsyncVideoWriter(
                filename, fourcc, rec_fps, (self.actual_w, self.actual_h)
            )
            if not self.async_recorder.start():
                print("[Warn] MP4V codec missing. Fallback to MJPG (.avi)")
                filename = self.generate_filename("avi")
                fourcc = cv2.VideoWriter_fourcc(*'MJPG')
                self.async_recorder = AsyncVideoWriter(
                    filename, fourcc, rec_fps, (self.actual_w, self.actual_h)
                )
                self.async_recorder.start()

    def save_snapshot(self, frame):
        name = self.generate_filename("jpg")
        cv2.imwrite(name, frame)
        print(f"\n[User] Snapshot Saved: {name}")

    def update_logic(self, frame):
        # FPS Calculation
        self.frame_count += 1
        elapsed = time.time() - self.start_time
        if elapsed >= 1.0:
            self.current_fps = self.frame_count / elapsed
            self.frame_count = 0
            self.start_time = time.time()
        
        # Draw OSD
        color = (0, 255, 0)
        status_text = f"RES: {self.actual_w}x{self.actual_h} | FPS: {self.current_fps:.1f}"
        
        if self.async_recorder and self.async_recorder.is_recording:
            color = (0, 0, 255) # Red for recording
            status_text += f" | Drops: {self.async_recorder.dropped_frames}"
            
            # Blink indicator
            if int(time.time() * 2) % 2 == 0:
                 cv2.circle(frame, (self.actual_w-30, 30), 10, color, -1)
            cv2.putText(frame, "REC", (self.actual_w-90, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        
        cv2.putText(frame, status_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    def cleanup(self):
        print("\n" + "="*40)
        print("[System] Shutting down...")
        
        # 1. Stop Recording safely
        if self.async_recorder:
            print("[System] Finalizing pending video...")
            self.async_recorder.stop()
        
        # 2. Release Camera
        if hasattr(self, 'cap') and self.cap.isOpened():
            self.cap.release()
            print("[System] Camera released.")
            
        # 3. Close Windows
        cv2.destroyAllWindows()
        print("[System] Bye.")
        print("="*40)

    def run(self):
        print("\n" + "="*40)
        print(" Arducam Professional Recorder v7")
        print(" Controls: [V] Video  [S] Snapshot  [Q] Quit")
        print("="*40 + "\n")
        
        self.start_time = time.time()
        
        try:
            while self.is_running:
                ret, frame = self.cap.read()
                if not ret:
                    print("[Error] Device stream lost.")
                    break
                
                # --- One-Shot Hardware Config ---
                if not self.hw_configured:
                    self.apply_stream_active_controls()
                    self.hw_configured = True
                    # Reset Stats to avoid erratic initial FPS reading
                    self.start_time = time.time()
                    self.frame_count = 0
                # -------------------------------
                
                # 1. Push frame to recorder queue
                if self.async_recorder: 
                    self.async_recorder.write(frame)
                
                # 2. Display Preview (Copy needed so we don't draw on recorded video)
                disp = frame.copy()
                self.update_logic(disp)
                cv2.imshow("Arducam v7", disp)
                
                # 3. Input Handling
                k = cv2.waitKey(1) & 0xFF
                if k == ord('q'): 
                    self.is_running = False
                elif k == ord('v'): 
                    self.toggle_recording()
                elif k == ord('s'): 
                    self.save_snapshot(frame)

        except KeyboardInterrupt:
            print("\n[System] Interrupted by User (Ctrl+C).")
        finally:
            self.cleanup()

if __name__ == "__main__":
    app = CameraApp()
    app.run()
