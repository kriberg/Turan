#!/usr/bin/env python
import datetime
from xml.etree import ElementTree

class SXMLEntry(object):
    def __init__(self, time, hr, altitude):
        self.time = time
        self.hr = hr
        self.altitude = altitude
    def __unicode__(self):
        return '[%s] hr: %s, altitude: %s' % (self.time, self.hr, self.altitude)


class LapData(object):
    def __init__(self, start_time, duration, distance,  max_speed, avg_hr, max_hr, avg_cadence, kcal_sum):
        self.start_time = start_time
        self.duration = duration
        self.distance = distance
        self.max_speed = max_speed
        self.avg_hr = avg_hr
        self.max_hr = max_hr
        self.avg_cadence = avg_cadence
        self.kcal_sum = kcal_sum
        self.kcal = self.kcal_sum # alias

class SuuntoXMLParser(object):
    entries = []
    laps = []
    start_time = 0
    date = 0
    interval = 0
    duration = 0
    max_hr = 0
    avg_hr = 0
    kcal_sum = 0
    distance = 0
    comment = ''

    def parse_uploaded_file(self, f):
        t = ElementTree.parse(f)

        # The Suunto XML files can contain several exercises, each
        # having several laps. Can't do much about this, so let's 
        # just select the exercise with index 0
        move = t.find('.//Move[@Index="0"]')


        timestamp = move.find('.//Header/Time').text
        self.start_time = datetime.datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        self.time = self.start_time
        self.cur_time = self.time
        self.date = self.start_time.date()

        try:
            parts = map(float, move.find('.//Header/Duration').text.split(':'))
            parsed_duration =  datetime.timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2])
            self.duration = parsed_duration.total_seconds()
        except:
            pass

        try:
            move_max_hr = int(move.find('.//Header/HRMax').text)
        except:
            move_max_hr = 0

        try:
            move_avg_hr = int(move.find('.//Header/HRAvg').text)
        except:
            move_avg_hr = 0

        try:
            self.kcal_sum = int(move.find('.//Header/Calories').text)
        except:
            self.kcal_sum = 0

        try:
            move_distance = float(move.find('.//Header/Distance').text)
        except:
            move_distance = 0
        self.distance_sum = move_distance


        hr_samples = move.find('.//Samples/HR').text.strip().split(' ')[1:]
        alt_samples = move.find('.//Samples/Altitude').text.strip().split(' ')

        try:
            sample_rate = float(move.find('.//Header/SampleRate').text)
        except:
            sample_rate = float(self.duration)/float(len(hr_samples))

        time_acc = 0
        for i in range(0, min(len(hr_samples), len(alt_samples))):
            entry_time = self.start_time + datetime.timedelta(seconds=time_acc)
            self.entries.append(SXMLEntry(hr=hr_samples[i],
                    altitude=alt_samples[i],
                    time=entry_time))
            time_acc += sample_rate


        marks = move.findall('.//Marks/Mark')
        elapsed_time = datetime.timedelta()
        for mark in marks:
            try:
                parts = map(float, mark.find('Time').text.split(':'))
                lap_duration =  datetime.timedelta(hours=parts[0],
                        minutes=parts[1],
                        seconds=parts[2])
            except:
                lap_duration = datetime.timedelta()
            start_time = self.start_time + elapsed_time

            try:
                avg_hr = int(mark.find('HRAvg').text)
            except:
                avg_hr = move_avg_hr

            try:
                avg_cadence = int(mark.find('Cadence').text)
            except:
                avg_cadence = 0

            try:
                max_speed = float(mark.find('MaxSpeed').text)
            except:
                max_speed = 0

            try:
                distance = float(mark.find('Distance').text)
            except:
                distance = move_distance

            # Finding max_hr is a bit tricky, as it isnt recorded
            # in the header. We need to find the samples belonging to
            # this lap and then find the max. This won't be NDeGT precise.
            if len(self.laps) == 0:
                first_sample = 0
            else:
                offset_seconds = (start_time - self.start_time).total_seconds()
                first_sample = int(float(offset_seconds)/sample_rate)

            if lap_duration.total_seconds() == self.duration:
                last_sample = len(hr_samples)
            else:
                last_sample = int(float(lap_duration.total_seconds())/sample_rate)
            try:
                max_hr = max(hr_samples[first_sample:last_sample])
            except:
                max_hr = move_max_hr

            ld = LapData(start_time=start_time,
                    duration=lap_duration.total_seconds(),
                    distance=distance,
                    max_speed = max_speed,
                    avg_hr=avg_hr,
                    max_hr=max_hr,
                    avg_cadence=avg_cadence,
                    kcal_sum = self.kcal_sum)

            elapsed_time += lap_duration
            self.laps.append(ld)




if __name__ == '__main__':
    import sys
    print sys.argv[1]
    s = SuuntoXMLParser()
    s.parse_uploaded_file(file(sys.argv[1]))
    print 'Laps: %d' % len(s.laps)
    print 'Time, altitude, Hr'
    for x in s.entries:
        print x.time, x.altitude, x.hr
    print 'Start time: ', s.start_time
    print 'Date: ', s.date
    print 'Distance: ', s.distance
    print 'Samples: ', len(s.entries)
    print 'Duration: ', s.duration
