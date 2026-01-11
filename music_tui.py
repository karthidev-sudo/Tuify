import curses
import requests
import subprocess
import threading
import shutil
import sys
import time
import socket
import json
import os
import re
from urllib.parse import quote_plus

# --- Configuration ---
ITUNES_API_URL = "https://itunes.apple.com/search?term={}&media=music&limit=50"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}
SOCKET_PATH = "/tmp/mpv_tui_socket"
META_FILE = "/tmp/music_tui_current"

# MPV Command
MPV_CMD = [
    "mpv",
    "--no-video",
    "--no-terminal",
    f"--input-ipc-server={SOCKET_PATH}", 
    "--keep-open=no",
    "--idle=no"
]

class AudioPlayer:
    def __init__(self):
        self.process = None
        self.current_song = None
        self.is_playing = False
        self.is_paused = False
        self.manually_stopped = False
        # Cleanup old socket
        if os.path.exists(SOCKET_PATH):
            try: os.remove(SOCKET_PATH)
            except: pass

    def update_metadata_file(self):
        """Saves current song info for Rofi to read."""
        if self.current_song:
            # Format: Track - Artist
            t = self.current_song.get('track', 'Unknown')
            a = self.current_song.get('artist', 'Unknown')
            # If it's a YouTube mix result, sometimes artist is uploader
            content = f"{t} - {a}"
        else:
            content = "Not Playing"
            
        try:
            with open(META_FILE, "w") as f:
                f.write(content)
        except:
            pass

    def play(self, youtube_url, song_info):
        self.stop()
        
        self.manually_stopped = False
        self.current_song = song_info
        self.is_playing = True
        self.is_paused = False
        
        # Update Rofi Info
        self.update_metadata_file()

        try:
            self.process = subprocess.Popen(
                MPV_CMD + [youtube_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(0.3)
            # Safety Check
            if self.process.poll() is not None:
                self.is_playing = False
                self.process = None
        except FileNotFoundError:
            self.is_playing = False

    def send_socket_command(self, command_list):
        if not os.path.exists(SOCKET_PATH): return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(SOCKET_PATH)
            cmd = {"command": command_list}
            sock.sendall(json.dumps(cmd).encode() + b'\n')
            sock.close()
        except Exception:
            pass

    def toggle_pause(self):
        if self.is_playing:
            self.send_socket_command(["cycle", "pause"])
            self.is_paused = not self.is_paused

    def stop(self):
        self.manually_stopped = True
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=0.2)
            except:
                self.process.kill()
        
        self.process = None
        self.is_playing = False
        self.is_paused = False
        self.current_song = None
        
        # Clear metadata on stop
        self.update_metadata_file()
        
        if os.path.exists(SOCKET_PATH):
            try: os.remove(SOCKET_PATH)
            except: pass

    def check_status(self):
        if self.process:
            if self.process.poll() is not None:
                self.is_playing = False
                self.process = None
                return not self.manually_stopped
        return False

class MusicTUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.player = AudioPlayer()
        self.results = []
        self.selected_index = 0
        self.search_term = ""
        self.status_message = "Press 's' to search."
        self.loading = False
        self.autoplay = True
        self.mode = "SEARCH"
        
        # --- Theme ---
        curses.start_color()
        curses.use_default_colors()
        curses.curs_set(0)
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
        curses.init_pair(6, curses.COLOR_RED, -1)

        self.run()

    def safe_addstr(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if y >= h or x >= w: return
        max_len = w - x - 1
        if len(text) > max_len: text = text[:max_len]
        try:
            self.stdscr.addstr(y, x, text, attr)
        except: pass

    def clean_artist_name(self, artist_raw):
        """Fallback cleaner for iTunes search."""
        separators = ["&", "feat.", "Feat.", "ft.", ",", " x "]
        cleaned = artist_raw
        for sep in separators:
            if sep in cleaned:
                cleaned = cleaned.split(sep)[0]
        return cleaned.strip()

    def extract_video_id(self, url):
        match = re.search(r'v=([a-zA-Z0-9_-]{11})', url)
        if match: return match.group(1)
        match = re.search(r'youtu\.be/([a-zA-Z0-9_-]{11})', url)
        if match: return match.group(1)
        return None

    def fetch_itunes_results(self, query):
        try:
            url = ITUNES_API_URL.format(quote_plus(query))
            resp = requests.get(url, headers=HEADERS, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            
            clean_results = []
            seen = set()
            for item in data.get('results', []):
                t = item.get('trackName', 'Unknown')
                a = item.get('artistName', 'Unknown')
                key = f"{t.lower()}{a.lower()}"
                if key not in seen:
                    seen.add(key)
                    clean_results.append({'track': t, 'artist': a})
            
            return clean_results, None
        except Exception as e:
            return [], str(e)

    def fetch_youtube_mix(self, video_id):
        try:
            mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
            cmd = [
                "yt-dlp",
                "--flat-playlist", 
                "--playlist-end", "25",
                "--print", "%(id)s:::%(title)s:::%(uploader)s",
                mix_url
            ]
            output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8')
            
            new_results = []
            seen_ids = set()
            for line in output.splitlines():
                if ":::" not in line: continue
                parts = line.split(":::")
                if len(parts) < 3: continue
                
                vid_id, title, uploader = parts[0], parts[1], parts[2]
                if vid_id == video_id: continue
                
                if vid_id not in seen_ids:
                    seen_ids.add(vid_id)
                    new_results.append({
                        'track': title,
                        'artist': uploader,
                        'video_id': vid_id
                    })
            return new_results
        except Exception:
            return []

    def get_youtube_url(self, query):
        try:
            cmd = [
                "yt-dlp", 
                f"ytsearch1:{query} audio", 
                "--print", "webpage_url", 
                "--no-playlist", 
                "--ignore-errors", 
                "--no-warnings"
            ]
            return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=12).decode('utf-8').strip()
        except: 
            return None

    def draw_screen(self):
        self.stdscr.clear()
        h, w = self.stdscr.getmaxyx()

        # Header Info
        display_mode = self.mode
        if self.mode == "RADIO":
             if self.player.current_song:
                 display_mode = f"RADIO: {self.player.current_song.get('track', 'Mix')}"

        title = f" ARCH MUSIC TUI | Last Search: {self.search_term}"
        
        self.stdscr.attron(curses.color_pair(4))
        self.stdscr.move(0, 0)
        self.stdscr.clrtoeol()
        self.safe_addstr(0, 0, title, curses.color_pair(4))
        
        ap_text = f" [{display_mode}] " 
        if not self.autoplay: ap_text = " [AUTOPLAY: OFF] "
        
        ap_attr = curses.color_pair(5) if self.mode == "RADIO" else curses.color_pair(4)
        self.safe_addstr(0, len(title) + 1, ap_text, ap_attr)
        
        controls = "Controls: [Space]Pause [n]Next(Smart) [b]Back [x]Stop [s]Search"
        self.safe_addstr(1, 0, controls, curses.color_pair(3))
        self.stdscr.attroff(curses.color_pair(4))

        # List
        max_rows = h - 5
        if max_rows > 0:
            start = max(0, self.selected_index - max_rows // 2)
            end = min(len(self.results), start + max_rows)
            for i, idx in enumerate(range(start, end)):
                item = self.results[idx]
                if 'video_id' in item:
                    line = f"{item['track']}" 
                else:
                    line = f"{item['track']} - {item['artist']}"

                attr = 0
                is_current = False
                if self.player.current_song and self.player.current_song['track'] == item['track']:
                    is_current = True

                if idx == self.selected_index:
                    prefix = "> "
                    attr = curses.color_pair(1) | curses.A_BOLD
                elif is_current:
                    prefix = "* "
                    attr = curses.color_pair(2)
                else:
                    prefix = "  "

                self.safe_addstr(i + 3, 2, f"{prefix}{line}", attr)

        # Status Bar
        status_y = h - 1
        if self.loading:
            self.safe_addstr(status_y, 0, f"[ ... {self.status_message} ... ]", curses.color_pair(3))
        elif self.player.is_playing:
            s = self.player.current_song
            status_icon = "[PAUSED]" if self.player.is_paused else "[PLAYING]"
            disp_song = s['track']
            if 'artist' in s and s['artist'] != "YouTube Mix":
                disp_song += f" - {s['artist']}"
                
            color = curses.color_pair(6) | curses.A_BOLD if self.player.is_paused else curses.color_pair(2)
            self.safe_addstr(status_y, 0, f"{status_icon} {disp_song}", color)
        else:
            self.safe_addstr(status_y, 0, self.status_message, curses.color_pair(3))

        self.stdscr.refresh()

    def custom_input(self):
        curses.curs_set(1)
        buffer = []
        while True:
            _, w = self.stdscr.getmaxyx()
            self.stdscr.move(0, 0)
            self.stdscr.attron(curses.color_pair(4))
            self.stdscr.clrtoeol()
            display = " Search: " + "".join(buffer)
            self.safe_addstr(0, 0, display, curses.color_pair(4))
            self.stdscr.attroff(curses.color_pair(4))
            
            key = self.stdscr.getch()
            if key in [10, 13]: break
            elif key == 27: 
                curses.curs_set(0)
                return None
            elif key in [curses.KEY_BACKSPACE, 127]:
                if buffer: buffer.pop()
            elif 32 <= key <= 126: buffer.append(chr(key))
        curses.curs_set(0)
        return "".join(buffer)

    def perform_search(self):
        query = self.custom_input()
        if not query:
            self.draw_screen()
            return
        self.search_term = query
        self.loading = True
        self.mode = "SEARCH"
        self.status_message = "Searching iTunes..."
        self.draw_screen()
        
        results, err = self.fetch_itunes_results(query)
        self.loading = False
        
        if err: self.status_message = "Network Error."
        elif not results:
            self.status_message = "No songs found."
            self.results = []
        else:
            self.results = results
            self.selected_index = 0
            self.status_message = f"Found {len(results)} songs."

    def play_selection(self, song_item=None):
        if song_item:
            selected = song_item
        else:
            if not self.results: return
            if self.selected_index >= len(self.results): return
            selected = self.results[self.selected_index]

        self.loading = True
        self.status_message = f"Fetching: {selected['track']}..."
        self.draw_screen()
        
        def _bg():
            url = None
            if 'video_id' in selected:
                url = f"https://www.youtube.com/watch?v={selected['video_id']}"
            else:
                query = f"{selected['track']} {selected['artist']} audio"
                url = self.get_youtube_url(query)
                if url:
                    vid_id = self.extract_video_id(url)
                    if vid_id: selected['video_id'] = vid_id
            
            if url:
                self.loading = False
                self.player.play(url, selected)
                time.sleep(1.0)
                if not self.player.is_playing and self.autoplay:
                     self.status_message = "Playback failed. Skipping..."
                     self.handle_smart_navigation(explicit_prev_song=selected)
            else:
                self.loading = False
                self.status_message = "Song Unavailable. Skipping..."
                time.sleep(1.0)
                if self.autoplay:
                     self.handle_smart_navigation(explicit_prev_song=selected)

        threading.Thread(target=_bg, daemon=True).start()

    def start_smart_radio(self, prev_song):
        self.loading = True
        self.status_message = "Generating Smart Radio..."
        self.draw_screen()
        
        def _bg_smart():
            mix_found = False
            if 'video_id' in prev_song:
                self.status_message = "Fetching YouTube Recommendations..."
                mix_results = self.fetch_youtube_mix(prev_song['video_id'])
                if mix_results:
                    self.results = mix_results
                    self.mode = "RADIO"
                    self.search_term = "Smart Mix"
                    self.selected_index = 0
                    mix_found = True
            
            if not mix_found:
                clean_art = self.clean_artist_name(prev_song['artist'])
                self.status_message = f"Falling back to Artist: {clean_art}..."
                res, _ = self.fetch_itunes_results(clean_art)
                if res:
                    self.results = res
                    self.mode = "RADIO"
                    self.search_term = f"Artist: {clean_art}"
                    next_idx = 0
                    if prev_song:
                         last_clean = prev_song['track'].lower().split('(')[0]
                         for i, s in enumerate(self.results):
                             if s['track'].lower().split('(')[0] != last_clean:
                                 next_idx = i
                                 break
                    self.selected_index = next_idx
                    mix_found = True
            
            self.loading = False
            if mix_found:
                self.play_selection()
            else:
                self.status_message = "Could not generate radio."

        threading.Thread(target=_bg_smart, daemon=True).start()

    def handle_smart_navigation(self, explicit_prev_song=None):
        prev_song = explicit_prev_song if explicit_prev_song else self.player.current_song
        if not prev_song: return

        if self.mode == "SEARCH":
            self.start_smart_radio(prev_song)
        elif self.mode == "RADIO":
            if self.selected_index < len(self.results) - 1:
                self.selected_index += 1
                self.play_selection()
            else:
                self.start_smart_radio(prev_song)

    def skip_next(self):
        last_known_song = self.player.current_song
        if last_known_song:
            self.player.stop()
            self.handle_smart_navigation(explicit_prev_song=last_known_song)
        else:
            self.play_selection()

    def skip_prev(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self.play_selection()
        else:
            self.status_message = "Start of playlist."

    def run(self):
        self.stdscr.timeout(100) 
        while True:
            if self.player.check_status():
                if self.autoplay:
                    self.handle_smart_navigation()
            
            self.draw_screen()
            try:
                key = self.stdscr.getch()
            except KeyboardInterrupt:
                break
            
            if key == curses.ERR: continue
            
            if key == ord('q'):
                self.player.stop()
                break
            elif key == curses.KEY_UP:
                if self.selected_index > 0: self.selected_index -= 1
            elif key == curses.KEY_DOWN:
                if self.selected_index < len(self.results) - 1: self.selected_index += 1
            elif key == ord('s') or key == ord('/'): 
                self.player.stop()
                self.perform_search()
            elif key in [10, 13, curses.KEY_ENTER]: 
                self.play_selection()
            elif key == ord(' '): 
                self.player.toggle_pause()
            elif key == ord('n'): 
                self.skip_next()
            elif key == ord('b'): 
                self.skip_prev()
            elif key == ord('x'): 
                self.player.stop()
            elif key == ord('a'): 
                self.autoplay = not self.autoplay

def main():
    if not shutil.which("yt-dlp"):
        print("Error: yt-dlp is required.")
        return
    if not shutil.which("mpv"):
        print("Error: mpv is required.")
        return
        
    try:
        curses.wrapper(MusicTUI)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()