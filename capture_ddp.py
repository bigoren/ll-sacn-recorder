import argparse
from datetime import datetime
import socket
import struct

version = "2.0.0"

UDP_PORT = 4048

def check_positive_int(value):
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("{} is an invalid positive int value".format(value))
    return ivalue

def is_empty_bytearray(input_data: bytearray):
    byte_length = len(input_data)
    if input_data.count(0) == byte_length:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Listen for DDP packets, determine string_len for N strings and capture f frames of pixel data for those strings")
    parser.add_argument("-s", "--strings", dest="number_of_strings", action='store', type=check_positive_int, default=1,
                        help="number of LED strings to learn string_len for and capture frames for")
    parser.add_argument("-o", "--output", dest="output", type=str, required=True,
                        help="output file to write the max string length header to")
    parser.add_argument('-t', '--seconds_to_capture', dest='seconds_to_capture', action='store', type=check_positive_int,
                        help='if set, app will exit after capturing for this many seconds')
    parser.add_argument('-d', '--debug', dest='debug', action='store_true',
                        help='enable debug output')
    args = parser.parse_args()

    # Create UDP listener
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    # Make recvfrom interruptible on Windows and responsive to Ctrl-C
    # Use a short timeout so we can catch KeyboardInterrupt reliably
    sock.settimeout(1.0)

    print(f"Listening for DDP packets on 0.0.0.0:{UDP_PORT} ...")
    print(f"Will stop after learning string_len for {args.number_of_strings} LED string(s) (detected via PUSH packets)")

    CHANNELS_PER_PIXEL = 3
    strings_ranges = {}  # assigned_string -> (start, end) offsets
    next_assigned = 1  # running string number assignment
    packets_seen = 0
    start = 0
    is_beginning = True
    empty_frames = 0
    collecting = False
    buffers = {}  # assigned_string -> bytearray buffer
    filled_count = {}  # assigned_string -> int count of filled bytes
    max_bytes = None
    last_frame_time = None

    try:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                # Check if we've been capturing and haven't received a frame in 5 seconds
                if collecting and not is_beginning and last_frame_time is not None:
                    time_since_last_frame = (datetime.now() - last_frame_time).total_seconds()
                    if time_since_last_frame >= 5.0:
                        elapsed_time_ms = (last_frame_time - start_time).total_seconds() * 1_000
                        fps = 1_000 * frames_written / elapsed_time_ms if frames_written > 0 else 0
                        print(f"\nNo frames received for 5 seconds. Exiting...")
                        print(f"Captured {frames_written} frames in {elapsed_time_ms:.1f}ms")
                        if frames_written > 0:
                            print(f"Average frame rate: {fps:.1f} fps")
                        return
                # loop back and allow KeyboardInterrupt to be raised
                continue

            # Ensure minimum header length
            if len(data) < 10:
                print(f"Packet too short ({len(data)} bytes), skipping...")
                continue

            # Parse DDP header fields
            flags1 = data[0]
            flags2 = data[1]
            data_type = data[2]
            string_id = data[3]  # reported string ID from packet (we don't use this)
            offset = struct.unpack(">I", data[4:8])[0]      # 32-bit MSB first
            length = struct.unpack(">H", data[8:10])[0]     # 16-bit MSB first

            # Check for timecode
            has_timecode = bool(flags1 & 0x10)
            timecode = None
            pos = 10
            if has_timecode:
                if len(data) >= 14:
                    timecode = struct.unpack(">I", data[10:14])[0]
                    pos = 14
                else:
                    print(f"Packet claims timecode but too short ({len(data)} bytes), skipping...")
                    continue
            # Pretty print the header fields
            packets_seen += 1
            if not collecting and args.debug:
                print(f"\nPacket {packets_seen} from {addr}:")
                print(f"  flags1:    0x{flags1:02x}  (version={(flags1 & 0xc0) >> 6}, "
                    f"time={'Y' if has_timecode else 'N'}, "
                    f"store={'Y' if flags1 & 0x08 else 'N'}, "
                    f"reply={'Y' if flags1 & 0x04 else 'N'}, "
                    f"query={'Y' if flags1 & 0x02 else 'N'}, "
                    f"push={'Y' if flags1 & 0x01 else 'N'})")
                print(f"  flags2:    0x{flags2:02x}  (sequence={flags2 & 0x0f})")
                print(f"  type:      0x{data_type:02x}")
                print(f"  id:        {string_id}")
                print(f"  offset:    {offset}")
                print(f"  length:    {length}")
                if has_timecode:
                    print(f"  timecode:  {timecode}")

            # Validate payload length (best-effort)
            payload = data[pos:pos + length]
            if len(payload) != length:
                print(f"  Warning: expected payload length {length} but got {len(payload)}")
                continue

            # First stage, before we collect any data, learn the string ranges
            # If PUSH flag set, assign a new internal string number (running number)
            push_flag = bool(flags1 & 0x01)
            if not collecting and push_flag:
                end = offset + length
                # assign new running string number regardless of remote or reported string_id
                assigned = next_assigned
                strings_ranges[assigned] = (start, end)
                if args.debug:
                    print(f"  PUSH detected: assigned string {assigned} range=({start}, {end}) length={end - start}")
                next_assigned += 1
                start = end

            # Check when we've learned all string ranges
            if not collecting and len(strings_ranges) >= args.number_of_strings:
                print(f"\nLearned string ranges for {len(strings_ranges)} strings.")
                print("Summary:")
                for string_num, (s, e) in strings_ranges.items():
                    print(f"  string {string_num}: range=({s},{e}) length={e - s}")

                # Find max string length in pixels and write to output file
                max_string_len = max(e - s for (s, e) in strings_ranges.values())
                max_pixels = max_string_len // CHANNELS_PER_PIXEL

                # Write 2-byte header with max pixels (little-endian)
                with open(args.output, "wb") as f:
                    f.write(max_pixels.to_bytes(2, byteorder="little"))
                    print(f"Wrote max pixels={max_pixels} as 2-byte header to {args.output}")

                # Allocate buffers for each string
                for string_num, (s, e) in strings_ranges.items():
                    length_bytes = e - s
                    buffers[string_num] = bytearray(length_bytes)
                    # initialize filled count
                    filled_count[string_num] = 0
                # compute maximum bytes for padding when writing full frames
                max_string_len = max(e - s for (s, e) in strings_ranges.values())
                max_pixels = max_string_len // CHANNELS_PER_PIXEL
                max_bytes = max_pixels * CHANNELS_PER_PIXEL
                collecting = True
                print("Waiting for non empty packets to start capturing frames...")
                frames_written = 0  # initialize frame counter
                # continue listening; do not break

            # don't record empty byte arrays at beginning of file.
            if is_beginning and is_empty_bytearray(payload):
                empty_frames += 1
                continue
            elif is_beginning:
                # The recording starts here
                print("skipped {} empty frames, starting real capture".format(empty_frames))
                is_beginning = False
                start_time = datetime.now()
                last_frame_time = datetime.now()

            # If we are collecting, attempt to write payloads into the appropriate buffer(s)
            if collecting:
                # For each known string, check whether this packet fits entirely inside its range
                written = False
                for string_num, (s, e) in strings_ranges.items():
                    if offset >= s and (offset + length) <= e:
                        rel_off = offset - s
                        # write payload into buffer and count bytes
                        buffers[string_num][rel_off:rel_off + length] = payload
                        filled_count[string_num] += length
                        written = True
                        break
                if not written:
                    print(f"Packet offset {offset}+{length} does not fit any string range; ignoring.")
                else:
                    # After a successful write, check if all strings are fully filled
                    all_full = all(filled_count.get(n, 0) == len(buffers[n]) for n in buffers)
                    if all_full:
                        # assemble concatenated frame, padding each buffer to max_bytes
                        frame_parts = []
                        for n in sorted(buffers.keys()):
                            b = buffers[n]
                            if max_bytes is None:
                                part = bytes(b)
                            else:
                                if len(b) < max_bytes:
                                    part = bytes(b) + b"\x00" * (max_bytes - len(b))
                                else:
                                    part = bytes(b[:max_bytes])
                            frame_parts.append(part)
                        frame_data = b"".join(frame_parts)

                        # calculating the time delta from start to now in milliseconds to use as time header
                        cur_time = datetime.now() - start_time
                        time_ms = int(cur_time.total_seconds() * 1_000)
                        time_header = time_ms.to_bytes(4, 'little')

                        # append to output file
                        with open(args.output, "ab") as f:
                            f.write(time_header + frame_data)

                        # Update last frame time
                        last_frame_time = datetime.now()

                        # Check if we've reached the time capture limit
                        frames_written += 1
                        if frames_written % 40 == 0:
                            elapsed_time_ms = (datetime.now() - start_time).total_seconds() * 1_000
                            fps = 1_000 * frames_written / elapsed_time_ms
                            print(f"Captured {frames_written} frames in {elapsed_time_ms:.1f}ms ({fps:.1f} fps)")
                        if args.seconds_to_capture:
                            elapsed_time_s = (datetime.now() - start_time).total_seconds()
                            if elapsed_time_s >= args.seconds_to_capture:
                                elapsed_time_ms = elapsed_time_s * 1_000
                                fps = 1_000 * frames_written / elapsed_time_ms
                                print(f"\nFinished! Captured {frames_written} frames in {elapsed_time_ms:.1f}ms")
                                print(f"Average frame rate: {fps:.1f} fps")
                                return

                        # reset buffers and filled counts for next frame
                        for n in buffers:
                            buffers[n][::] = b'\x00' * len(buffers[n])  # faster in-place zeroing
                            filled_count[n] = 0

    except KeyboardInterrupt:
        # Handle Ctrl-C gracefully
        print("\nReceived Ctrl-C, exiting...")
        try:
            sock.close()
        except Exception:
            pass
        return


if __name__ == "__main__":
    main()