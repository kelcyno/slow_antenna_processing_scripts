import argparse
from datetime import timezone

import cv2
import ffmpeg
import numpy as np
import polars as pl
import pynmea2
import scipy.signal
from scipy.interpolate import interp1d


if __name__ == '__main__':
    # Set up argparse
    parser = argparse.ArgumentParser('Parse GPS NMEA and PPS from a video\'s audio track')
    parser.add_argument('-i', '--input', required=True, help='Path to the input video file')
    parser.add_argument('-c', '--output-csv', default=None, help='Path to output a CSV file of frame numbers and times')
    parser.add_argument('-s', '--sample-rate', default=44100, type=int, help='Audio sample rate') # 44.1 kHz should be default for most
    parser.add_argument('-r', '--serial-bitrate', default=4800, type=int, help='Bitrate of the NMEA serial stream')
    parser.add_argument('-w', '--serial-bits-per-word', default=8, type=int, help='Bits per word of the NMEA serial stream')
    parser.add_argument('--serial-stopbits', default=1, type=int, help='Stop bits of the NMEA serial stream')
    parser.add_argument('-o', '--output-video', help='Path to the output video file')
    parser.add_argument('-t', default=None, type=str, help='Static text to overlay on all frames')
    args = parser.parse_args()
    input_video = args.input
    sample_rate = args.sample_rate
    num_channels = 2 # this was originally an arg but the rest of this script is really not designed to handle mono audio
    serial_bitrate = args.serial_bitrate
    serial_bits_per_word = args.serial_bits_per_word
    serial_stopbits = args.serial_stopbits

    # Run ffmpeg and capture raw PCM audio output
    print('Reading audio...')
    process = (
        ffmpeg.input(input_video)
        .output('pipe:', format='s16le', acodec='pcm_s16le', ac=num_channels, ar=sample_rate)
        .run(capture_stdout=True, capture_stderr=True)
    )

    # Convert raw bytes to NumPy array
    audio_bytes = process[0]
    audio_array = np.frombuffer(audio_bytes, dtype=np.int16)

    # Reshape to (num_samples, num_channels)
    audio_array = audio_array.reshape(-1, num_channels)

    # The PPS signal will have many more samples near zero than the GPS NMEA signal. Use this to determine which channel (L/R) is which.
    points_near_zero = np.sum(np.abs(audio_array) < 1000, axis=0)

    if np.abs(points_near_zero[0] - points_near_zero[1]) < audio_array.shape[0] * 0.5:
        raise ValueError('Unable to determine which channel is PPS and which is GPS NMEA.')
    else:
        if points_near_zero[0] > points_near_zero[1]:
            print('PPS appears to be left channel, NMEA appears to be right channel')
            pps_signal = audio_array[:, 0]
            gps_signal = audio_array[:, 1]
        else:
            print('PPS appears to be right channel, NMEA appears to be left channel')
            pps_signal = audio_array[:, 1]
            gps_signal = audio_array[:, 0]

    # Arrays of sample numbers and elapsed time since start of file
    sample_num = np.arange(audio_array.shape[0])
    sample_elapsed = sample_num / sample_rate

    # digitize the PPS signal
    pps_digitizer = np.max(np.abs(pps_signal)) / 4 # if the signal exceeds 25% of the max, it's probably a pulse
    pps_high_mask = np.abs(pps_signal) > pps_digitizer # All points where the PPS signal is in the mark state
    # Only consider "rising edge"... points that are at least 0.5 seconds since the last mark. The first mark is always considered a start.
    pps_digital_starts = np.append([True], (sample_num[pps_high_mask][1:] >= sample_num[pps_high_mask][:-1] + sample_rate * 0.5))
    # Make a mask of only the first points of the marks
    pps_digital_mask = np.zeros_like(pps_signal, dtype=bool)
    pps_digital_mask[pps_high_mask] = pps_digital_starts
    # Obtain the elapsed time and sample number of the PPS rising edges.
    pps_frames = sample_num[pps_digital_mask]
    pps_digital_elapsed = sample_elapsed[pps_digital_mask]
    pps_digital = pps_signal[pps_digital_mask]


    # digitize the NMEA stream
    gps_nonzero_mask = ~(np.abs(gps_signal) < np.max(np.abs(gps_signal)) / 10) # Mask of points where the GPS signal is not zero
    gps_nonzero_signal = gps_signal[gps_nonzero_mask]
    gps_nonzero_elapsed = sample_elapsed[gps_nonzero_mask]
    # Find the start of each NMEA packet by finding the first point where the signal is not close to zero
    gps_packet_starts = np.append([True], ((gps_nonzero_elapsed[1:] - gps_nonzero_elapsed[:-1]) > 0.1))
    gps_packet_ends = np.append(gps_packet_starts[1:], [False]) # The end of a packet is 1 index behind the start of the next packet
    first_gps_packet_start = np.nonzero(gps_packet_starts)[0][0]
    last_gps_packet_start = np.nonzero(gps_packet_starts)[0][-1]
    # Exclude the first and last packet start as they may be cut off by the start or end of the recording
    gps_packet_starts[first_gps_packet_start] = False
    gps_packet_starts[last_gps_packet_start] = False
    # Filter the serial signal to reduce noise
    filtwin = scipy.signal.windows.blackmanharris(sample_rate//100) # .01 second moving average, blackman-harris convolution kernel
    # This acts as a rolling average for comparing the serial signal to see if it is in mark or space
    # Due to the nature of audio sampling, the serial signal has some low frequency components, especially at the start
    # By comparing to a moving average instead of a fixed threshold or zero-crossing, we can avoid misclassifying bits
    gps_digitizer = scipy.signal.fftconvolve(gps_signal, filtwin/np.sum(filtwin), mode='same')
    gps_digital_mask = gps_nonzero_signal > gps_digitizer[gps_nonzero_mask] # Mask of points where the GPS signal is in the mark state

    # A few useful constants used in the digitization process
    sec_per_bit = serial_bitrate**(-1)
    samples_per_bit = int(sample_rate * sec_per_bit)
    bits_per_word = 1 + serial_bits_per_word + serial_stopbits # 1 start bit + 8 data bits + 1 stop bit


    print('Digitizing NMEA stream...')
    nmea_infos = [] # List containing NMEA strings
    nmea_frames = [] # List containing the start frame of each NMEA string
    for i, gps_packet_start in enumerate(np.nonzero(gps_packet_starts)[0]):
        gps_packet_end = np.nonzero(gps_packet_ends)[0][i+1] # the matching endpoint for this packet
        gps_packet_end_elapsed = gps_nonzero_elapsed[gps_packet_end] # the elapsed time of the end of this packet
        this_bit_elapsed = gps_nonzero_elapsed[gps_packet_start] + 0.5 * sec_per_bit # Set up for the while loop. Start at the first bit of the packet
        serial_string = '' # String to hold the NMEA messages once parsed
        while True:
            # This loop iterates once per word (start bit + data bits + stop bit) of the NMEA packet
            # With standard NMEA, this is 1 start bit, 8 data bits, and 1 stop bit, for a total of 10 bits
            bit_elapseds = np.arange(this_bit_elapsed, this_bit_elapsed + bits_per_word * sec_per_bit, sec_per_bit)[:bits_per_word] # This last slice is to avoid accidentally including 11 bits. Probably could use linspace here.
            # Find the closest sample to each bit elapsed time
            closest_bit = np.argmin(np.abs(gps_nonzero_elapsed - bit_elapseds[:, None]), axis=1)
            byte = gps_digital_mask[closest_bit] # Define an array containing the word we want to decode
            
            # Ensure that the start bit is present and valid
            if byte[0] != 0:
                # If the start bit is not detected, increment the elapsed time by 1 bit time and try again. This seemed to work well in testing.
                this_bit_elapsed += sec_per_bit
                continue
            if byte[-1] != 1:
                # If the stop bit is not detected, check to see if we are at the end of the packet.
                if np.all(byte == 0):
                    break
                # If this is not the end of the packet and no stop bit is detected, the clock has drifted too far and decoding is no longer possible.
                raise ValueError(f'No stop bit detected at elapsed time {this_bit_elapsed}!')
            else:
                # If a start and stop bit are found, great! We can proceed with decoding.
                # Janky implementation of a PLL
                # Select 1 second around the stop bit
                stop_bit_elapseds = np.array([bit_elapseds[-1]-sec_per_bit, bit_elapseds[-1]+sec_per_bit])
                stop_bit_limits = np.argmin(np.abs(gps_nonzero_elapsed - stop_bit_elapseds[:, None]), axis=1)
                # Find the points in the stop bit that are most clearly "signal high"
                unambig = gps_nonzero_signal[stop_bit_limits[0]:stop_bit_limits[1]] - gps_digitizer[gps_nonzero_mask][stop_bit_limits[0]:stop_bit_limits[1]] > 10000
                # Use the average of these points to adjust the bit time for clock drift
                this_bit_elapsed = np.mean(gps_nonzero_elapsed[stop_bit_limits[0]:stop_bit_limits[1]][unambig]) + sec_per_bit # This is the time of the start bit of the next word.
            byte = np.flip(byte[1:-1]) # serial data is sent LSB first
            binary_string = ''.join([str(int(bit)) for bit in byte]) # Convert to binary string
            ascii_value  = int(binary_string, 2) # Binary string to decimal integer
            ascii_char = chr(ascii_value) # Decimal integer to ASCII character
            serial_string += ascii_char # Add the decoded character to the string
        # Split the string into lines and add to the list of NMEA sentences from this packet
        new_infos = [this_string.replace('\r', '') for this_string in serial_string.split('\n') if this_string != '']
        # Add this packet's sentences to the list of NMEA sentences
        nmea_infos.extend(new_infos)
        # Add this packet's start frame to the list of NMEA sentence times
        nmea_frames.extend([gps_packet_start] * len(new_infos))


    nmea_times = []
    nmea_lats = []
    nmea_lons = []
    for info in nmea_infos:
        try:
            this_nmea_msg = pynmea2.parse(info)
            try:
                this_nmea_time = this_nmea_msg.datetime
            except AttributeError:
                this_nmea_time = None
            try:
                this_nmea_lat = this_nmea_msg.latitude
            except AttributeError:
                this_nmea_lat = None
            try:
                this_nmea_lon = this_nmea_msg.longitude
            except AttributeError:
                this_nmea_lon = None
        except pynmea2.ChecksumError as e:
            this_nmea_msg = None
        nmea_times.append(this_nmea_time)
        nmea_lats.append(this_nmea_lat)
        nmea_lons.append(this_nmea_lon)
        

    # Keep only the NMEA sentences with valid times, and drop duplicates
    unique_nmea_times = []
    unique_nmea_frames = []
    unique_nmea_lats = []
    unique_nmea_lons = []
    for i, this_nmea_time in enumerate(nmea_times):
        if this_nmea_time is not None:
            this_nmea_frame = nmea_frames[i]
            this_nmea_lat = nmea_lats[i]
            this_nmea_lon = nmea_lons[i]
            if this_nmea_time not in unique_nmea_times:
                unique_nmea_times.append(this_nmea_time.astimezone(timezone.utc).replace(tzinfo=None))
                unique_nmea_frames.append(this_nmea_frame)
                unique_nmea_lats.append(this_nmea_lat)
                unique_nmea_lons.append(this_nmea_lon)
    unique_nmea_times = np.array(unique_nmea_times).astype('datetime64[s]')
    unique_nmea_frames = np.array(unique_nmea_frames)
    unique_nmea_lats = np.array(unique_nmea_lats)
    unique_nmea_lons = np.array(unique_nmea_lons)
    # Find the elapsed time of the unique NMEA frames
    unique_nmea_elapsed = gps_nonzero_elapsed[unique_nmea_frames]


    print('Associating NMEA and PPS...')
    # Associate each reported NMEA time with the previous PPS pulse time
    actual_nmea_elapsed = np.zeros_like(unique_nmea_elapsed)
    for i, uncorrected_nmea_elapsed in enumerate(unique_nmea_elapsed):
        previous_pps = pps_digital_elapsed[pps_digital_elapsed < uncorrected_nmea_elapsed][-1]
        actual_nmea_elapsed[i] = previous_pps

    # Use ffmpeg to read the frame times from the video
    frame_elapsed_json = ffmpeg.probe(input_video, select_streams='v', show_entries='frame=pts_time', of='json')
    frame_elapsed = np.array([frame['pts_time'] for frame in frame_elapsed_json['frames']], dtype=float)

    # Interpolate the NMEA times to the frame times
    tinterper = interp1d(actual_nmea_elapsed, unique_nmea_times.astype('datetime64[ns]').astype(float), fill_value='extrapolate')
    frame_absolute_times = tinterper(frame_elapsed).astype('datetime64[ns]')
    latinterper = interp1d(actual_nmea_elapsed, unique_nmea_lats, fill_value='extrapolate')
    frame_latitudes = latinterper(frame_elapsed)
    loninterper = interp1d(actual_nmea_elapsed, unique_nmea_lons, fill_value='extrapolate')
    frame_longitudes = loninterper(frame_elapsed)


    # Write the CSV if requested
    if args.output_csv is not None:
        print('Writing CSV...')
        pl.DataFrame({'frame' : np.arange(len(frame_absolute_times)), 'elapsed' : frame_elapsed, 'time' : frame_absolute_times,
                      'latitude' : frame_latitudes, 'longitude' : frame_longitudes}).write_csv(args.output_csv)
    
    # Overlay the times on the video if requested
    if args.output_video is not None:
        frame_texts = frame_absolute_times.astype(str)
        # Read the input video
        cap = cv2.VideoCapture(input_video)
        # set up mp4 writer
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(args.output_video, fourcc, cap.get(cv2.CAP_PROP_FPS), (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))))

        frame_index = 0
        while cap.isOpened():
            print(f'Overlaying: {frame_index/len(frame_texts)*100:.2f}%')
            ret, frame = cap.read()
            if not ret:
                break

            # Overlay the text on the frame
            text = f'{frame_texts[frame_index]} {frame_latitudes[frame_index]:.5f} {frame_longitudes[frame_index]:.5f}'
            font = cv2.FONT_HERSHEY_SIMPLEX
            bottomLeftCornerOfText = (10, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) - 10) # Bottom-left corner of the text string in the image
            fontScale = 1
            fontColor = (0, 0, 255) # Blue, Green, Red
            lineType = 2

            cv2.putText(frame, text, bottomLeftCornerOfText, font, fontScale, fontColor, lineType)
            if args.t is not None:
                (text_width, text_height), baseline = cv2.getTextSize(text, font, fontScale, 1)
                bottom_left_corner_of_header = (10, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) - (20 + text_height)) # 10 pixels above the previous text
                cv2.putText(frame, args.t, bottom_left_corner_of_header, font, fontScale, fontColor, lineType)

            # Write the frame to the output video
            out.write(frame)
            frame_index += 1

        cap.release()
        out.release()
        cv2.destroyAllWindows()
