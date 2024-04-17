# from models.Generator import Generator
# from models.midi2pianoroll import midi_to_piano_roll , listen4midi
# from models.CONST_VARS import CONST

from Generator import Generator
from midi2pianoroll import midi_to_piano_roll , plot_multitrack
from CONST_VARS import CONST
import matplotlib.pyplot as plt

import torch
import numpy as np 
from pypianoroll import Multitrack, BinaryTrack

import pretty_midi
import mido
from time import time , ctime

import threading
import queue

import os

from Velocity_assigner.assign_velocity import VelocityAssigner

MIDI_OUT_PORT = 'IAC Driver Bus 2'
# MIDI_INPUT_PORT = 'Oxygen 61'
MIDI_INPUT_PORT = 'A-PRO 1'
# MIDI_INPUT_PORT = 'IAC Driver Bus 1'
TIME_WINDOW = 20
class Predictor:
    # WEIGHT_PATH = 'training_output_path_rootgenerator_20000.pth'
    # WEIGHT_PATH = 'models\generator_800.pth'
    # WEIGHT_PATH = os.path.join('models','generator_19900.pth')
    WEIGHT_PATH = os.path.join('models','generator_weights.pth')
    # WEIGHT_PATH = os.path.join('models','training_output_path_rootgenerator_20000.pth')
    GENRE = 'Pop_Rock' #'Latin' #
    EXECUTION_TIME = ctime(time()).replace(':','_').replace(' ','_')
    SAVE_PATH = os.path.join('static','midi',f'generated_drum_{GENRE}_{EXECUTION_TIME}')
    def __init__(self) -> None:
        self.generator = Generator()
        self.generator.load_state_dict(torch.load(Predictor.WEIGHT_PATH , map_location=torch.device('cpu'))) #TODO specific to cpu machine only
        self.generator.eval() #! this solve error thrown by data length

    
    def generate_drum(self, bass_piano_roll = None, tempo_array = None, bass_url = None):
        if not bass_url is None:
            bass_piano_roll , tempo_array = midi_to_piano_roll(bass_url)
        #TODO what is 64 here?
        #* padding to make output size divident of 64
        pad_size = 64 - bass_piano_roll.shape[0]%64
        # pad = np.zeros((pad_size , bass_piano_roll.shape[1])) #* zero padding which affects the generated music
        pad = bass_piano_roll[bass_piano_roll.shape[0]-pad_size: , :] #* padding with copying last frame
        bass_piano_roll = np.concatenate((bass_piano_roll,pad),axis=0)
        # bass_piano_roll = bass_piano_roll[:bass_piano_roll.shape[0]//64*64,:] #* no padding and cutting the excess part. Will generate better results but input and output are not of same size

        #* check to see if there were any out iof range pitch
        illegal_pitches_up = bass_piano_roll[:,:CONST.lowest_pitch]
        illegal_pitches_down = bass_piano_roll[:,CONST.lowest_pitch + CONST.n_pitches:]
        if not np.all(illegal_pitches_up == 0) :
            print("[-] Illegal up note")
        if not np.all(illegal_pitches_down == 0) :
            print("[-] Illegal down note")

        bass_piano_roll = torch.tensor(bass_piano_roll)
        bass_piano_roll = bass_piano_roll[:,CONST.lowest_pitch:CONST.lowest_pitch + CONST.n_pitches] 
        bass_piano_roll = bass_piano_roll.view(-1,64,72)

        latent = torch.randn(bass_piano_roll.shape[0], CONST.latent_dim)
        latent = latent.type(torch.float32)
        bass_piano_roll = bass_piano_roll.type(torch.float32)

        genre_tensor = torch.tensor(CONST.genre_code[Predictor.GENRE]).repeat((bass_piano_roll.shape[0])) #* repeat to the size of samples
        res = self.generator(latent,bass_piano_roll,genre_tensor)

        #* reshaping data inorder to be saved as image
        temp = torch.cat((res.cpu().detach(),bass_piano_roll.unsqueeze(1)),axis = 1).numpy()
        temp = temp.transpose(1,0,2,3)

        #TODO test: filling process time by repeating last measures
        temp = np.concatenate((temp,temp[:,-1:,:,:]),axis=1)

        temp = temp.reshape(temp.shape[0] , temp.shape[1] * temp.shape[2] , temp.shape[3])

        #* removing padding
        temp = temp[:, 0:temp.shape[1]-pad_size ,:]

        # plt.imshow(temp[1])

        print(f"[+][GENERATor] after bass_piano_roll shape {temp[1].shape}")

        tracks = []
        drum_track = []
        for idx, (program, is_drum, track_name) in enumerate(zip([0,33], [True,False], ['Drum','Bass'])):
            pianoroll = np.pad(temp[idx] > 0.5,((0, 0), (CONST.lowest_pitch, 128 - CONST.lowest_pitch - CONST.n_pitches)))
            tracks.append(BinaryTrack(name=track_name,program=program,is_drum=is_drum,pianoroll=pianoroll))

            if track_name == 'Drum':
                drum_track.append(BinaryTrack(name=track_name,program=program,is_drum=is_drum,pianoroll=pianoroll))


        multi_track = Multitrack(tracks=tracks,tempo=tempo_array,resolution=CONST.beat_resolution)
        multi_track_drum = Multitrack(tracks=drum_track,tempo=tempo_array,resolution=CONST.beat_resolution)

        plot_multitrack(multi_track.copy() , Predictor.SAVE_PATH+'.png')
        DB_midi = multi_track.to_pretty_midi()
        output_midi_name = Predictor.SAVE_PATH+'DB.midi'
        DB_midi.write(output_midi_name)

        output_midi_drum_name = Predictor.SAVE_PATH+'D.midi'
        multi_track_drum.to_pretty_midi().write(output_midi_drum_name)

        return DB_midi , output_midi_name , output_midi_drum_name
    
    def publish_midi(self):
        while not self.stop_listening :
            publish_duration = time()
            if self.processing_queue.qsize() == 0:
                continue
            pretty_midi_messages = self.processing_queue.get()  # Get messages from the queue
            self.processing_queue.task_done()  # Mark the task as done

            # pretty_midi_messages = pretty_midi.PrettyMIDI('test.midi')
            drum = None
            for instrument in pretty_midi_messages.instruments:
                if instrument.is_drum: #TODO debug
                    drum = instrument
                    break
            
            assert not (drum is None)

            # start_time = time()
            #     passed_time = time() - start_time
            mido_messages = []
            for note in drum.notes:
                mido_messages.append(mido.Message('note_on', note=note.pitch, velocity=note.velocity, time = note.start))
                mido_messages.append(mido.Message('note_off', note=note.pitch, velocity=0,time=note.end))

            mido_messages_sorted = sorted(mido_messages, key=lambda x: x.time)
            midi_duration = mido_messages_sorted[-1].time
            print(f"[+][PUBLISHER] MIDI duration {midi_duration} and number of midi messages {len(mido_messages_sorted)}")
            start_time = time()
            passed_time = time() - start_time
            message_counter = 0
            print(f"[+][PUBLISHER] from listening to publishing took {time()-self.process_begin_time}")
            #TODO bypassing while time check to allow generation of drum more than time window for jamming test purpose
            # while passed_time <= TIME_WINDOW:
            while True:
                passed_time = time() - start_time
                if mido_messages_sorted[message_counter].time - passed_time <=0.001:
                    self.midi_port_out.send(mido_messages_sorted[message_counter])
                    # print(f"[+][PUBLISHER] on time {passed_time} Sent {mido_messages_sorted[message_counter]}")
                    message_counter +=1
                if message_counter >= len(mido_messages_sorted):
                    break

            print(f"[+][PUBLISHER] publishing duration ----> {time()- publish_duration}")
    
    def listen(self):
        listen_start_time = time()
        pm_data = pretty_midi.PrettyMIDI()  # Create empty PrettyMIDI object
        bass = pretty_midi.Instrument(program=33) #! 33 is bass program code. got from CGAN repo
        start_time = time()
        end_time = start_time + TIME_WINDOW  # Listen for 10 seconds
        ons = {} #* this dictionary enables us to capture cords
        for message in self.midi_port_in:
            # print(f"[+][Listener] on time {time()-start_time} message {message}")
            if time() >= end_time:
                break
            # track.append(message)
            elif message.type == 'note_on':
                note_beg = time() - start_time
                ons[str(message.note)] = note_beg

            elif message.type == 'note_off':
                if str(message.note) in ons.keys():
                    note_end = time() - start_time
                    note = pretty_midi.Note(
                        velocity=message.velocity, #! this is not accurate becuse note on and note off velocities are different
                        pitch=message.note,
                        start=ons[str(message.note)],
                        end=note_end
                        )

                    bass.notes.append(note)  # Append note to first instrument

        self.process_begin_time = time()
        pm_data.instruments.append(bass)
        print(f"[+][Listener] number of midi messages listener received {len(bass.notes)} listening duration {time()-listen_start_time}")
        pm_data.write("static/midi/debug/debug_listened_midi.midi") #* no problem here
        return midi_to_piano_roll(midi_data = pm_data) #! problem

    def real_time_loop(self):
        self.stop_listening = False
        self.midi_port_out = mido.open_output(MIDI_OUT_PORT)
        self.midi_port_in = mido.open_input(MIDI_INPUT_PORT)
        self.lock = threading.Lock()
        self.processing_queue = queue.Queue()
        self.processing_thread = threading.Thread(target=self.publish_midi)
        self.processing_thread.start()

        while not self.stop_listening :
            print(f"[+] listening to bass for {TIME_WINDOW} seconds")
            piano_roll , tempo = self.listen()

            print("[+] generating drum")
            drum_midi , _ , _ = self.generate_drum(piano_roll , tempo)

            print("[+] sent to publish in queue")
            self.processing_queue.put(drum_midi)


def replace_drum(DB_path, D_path, output_path, vels):
    DB = pretty_midi.PrettyMIDI(DB_path)
    D = pretty_midi.PrettyMIDI(D_path)
    # D.set_resolution
    # DB.instruments[0] = D.instruments[0]
    for note_counter in range(len(DB.instruments[0].notes)):
        DB.instruments[0].notes[note_counter].velocity = vels[note_counter]
    DB.write('velocity_assigned_.midi')
    print('hi')


if __name__ == '__main__':
    p = Predictor()
    # t1 = p.generate_drum(bass_url = 'E:\Arash Workdir\Music-CGAN-app\static\\full_dataset_instance_cleaned-Contrabass_Bass.mid')

    # t1 , DB_path , D_path = p.generate_drum(bass_url = 'static\midi\sample_rock_song_from_dataset_DB.midi')
    # va = VelocityAssigner()
    # drum_with_velocity_path , vels = va.assing_velocity2midi(D_path)
    # DB_with_vel_path = 'DB_with_vel.midi'
    # replace_drum(DB_path , drum_with_velocity_path , DB_with_vel_path , vels)

    p.real_time_loop()
