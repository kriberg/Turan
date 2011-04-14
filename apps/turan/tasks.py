from celery.decorators import task
from celery.task.sets import subtask

from subprocess import call
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.conf import settings
from django.db.models import get_model
from django.db import connection, transaction
from django.db.models import Avg, Max, Min, Count, Variance, StdDev, Sum
from django.utils.datastructures import SortedDict

import numpy
from collections import deque

from svg import GPX2SVG
from gpxwriter import GPXWriter
from tcxwriter import TCXWriter
from gpxparser import GPXParser, proj_distance
from hrmparser import HRMParser
from gmdparser import GMDParser
from tcxparser import TCXParser
from csvparser import CSVParser
from pwxparser import PWXParser
from fitparser import FITParser
from polaronlineparser import POLParser


gpxstore = FileSystemStorage(location=settings.GPX_STORAGE)

def find_parser(filename):
    ''' Returns correctly initianted parser-class given a filename '''
    f_lower = filename.lower()

    if f_lower.endswith('.hrm'): # Polar !
        parser = HRMParser()
    elif f_lower.endswith('.gmd'): # garmin-tools-dump
        parser = GMDParser()
    elif f_lower.endswith('.tcx'): # garmin training centre
        parser = TCXParser(gps_distance=False) #should have menu on
                                               #upload page 
    elif f_lower.endswith('.csv'): # PowerTap
        parser = CSVParser()
    elif f_lower.endswith('.gpx'):
        parser = GPXParser()
    elif f_lower.endswith('.pwx'):
        parser = PWXParser()
    elif f_lower.endswith('.fit'):
        parser = FITParser()
    elif f_lower.endswith('.xml'): # Polar online
        parser = POLParser()
    else:
        raise Exception('Parser not found') # Maybe warn user somehow?
    return parser

def filldistance(values):
    d = 0
    if values:
        values[0].distance = 0
        d_check = values[len(values)-1].distance
        if d_check > 0:
            return d_check
        for i in xrange(1,len(values)):
            delta_t = (values[i].time - values[i-1].time).seconds
            if values[i].speed:
                d += values[i].speed/3.6 * delta_t
                values[i].distance = d
            else:
                values[i].distance = values[i-1].distance
    return d

def getavghr(values, start, end):
    hr = 0
    for i in xrange(start+1, end+1):
        delta_t = (values[i].time - values[i-1].time).seconds
        if values[i].hr:
            hr += values[i].hr*delta_t
    delta_t = (values[end].time - values[start].time).seconds
    return float(hr)/delta_t

def getavgpwr(values, start, end):
    pwr = 0
    for i in xrange(start+1, end+1):
        delta_t = (values[i].time - values[i-1].time).seconds
        try:
            pwr += values[i].power*delta_t
        except TypeError:
            return 0
    delta_t = (values[end].time - values[start].time).seconds
    return float(pwr)/delta_t

def calcpower(userweight, eqweight, gradient, speed,
        rollingresistance = 0.006 ,
        airdensity = 1.22 ,
        frontarea = 0.7 ):
    tot_weight = userweight+eqweight
    gforce = tot_weight * gradient/100 * 9.81
    frictionforce = rollingresistance * tot_weight * 9.81
    windforce = 0.5**2 * speed**2  * airdensity * frontarea
    return (gforce + frictionforce + windforce)*speed

@task
def getslopes(values, userweight):
    ''' Given values and weight at the time of exercise, find
    and calculate stats for slopes in exercise and save to db, returning
    the slopes found. Deletes any existing slopes for exercise '''
    Slope = get_model('turan', 'Slope')

    # Make sure we don't create duplicate slopes
    values[0].exercise.slope_set.all().delete()

    # Make sure exercise type is cycling, this only makes sense for cycling
    exercise_type = values[0].exercise.exercise_type
    if not str(exercise_type) == 'Cycling':
        return []
    if not filldistance(values):
        return []

    slopes = []
    min_slope = 40
    cur_start = 0
    cur_end = 0
    stop_since = False
    inslope = False
    for i in xrange(1,len(values)):
        if values[i].speed < 0.05 and not stop_since:
            stop_since = i
        if values[i].speed >= 0.05:
            stop_since = False
        if inslope:
            if values[i].altitude > values[cur_end].altitude:
                cur_end = i
            hdelta = values[cur_end].altitude - values[cur_start].altitude
            if stop_since:
                stop_duration = (values[i].time - values[stop_since].time).seconds
            else:
                stop_duration = 0
            try:
                if (values[i+1].time - values[i].time).seconds > 60:
                    stop_duration = max( \
                            (values[i+1].time - values[i].time).seconds, \
                            stop_duration )
            except IndexError:
                pass
            if values[i].altitude < values[cur_start].altitude + hdelta*0.9 \
                    or i == len(values)-1 \
                    or stop_duration > 60:
                if stop_duration > 60:
                    cur_stop = stop_since
                inslope = False
                if hdelta >= min_slope:
                    distance = values[cur_end].distance - values[cur_start].distance
                    if distance > 10:
                        slope = Slope(exercise=values[cur_start].exercise,
                                    start=values[cur_start].distance/1000,
                                    length = distance,
                                    ascent = hdelta,
                                    grade = hdelta/distance * 100)
                        slope.duration = (values[cur_end].time - values[cur_start].time).seconds
                        slope.speed = slope.length/slope.duration * 3.6
                        slope.avg_hr = getavghr(values, cur_start, cur_end)
                        slope.est_power = calcpower(userweight, 10, slope.grade, slope.speed/3.6)
                        slope.act_power = getavgpwr(values, cur_start, cur_end)

                        slope.start_lat = values[cur_start].lat
                        slope.start_lon = values[cur_start].lon
                        slope.end_lat = values[cur_end].lat
                        slope.end_lon = values[cur_end].lon

                        # Sanity check
                        if not slope.grade > 100:
                            slope.save()
                            slopes.append(slope)
                cur_start = i+1
        elif values[i].altitude <= values[cur_start].altitude:
            cur_start = i
            cur_end  = i
        elif values[i].altitude > values[cur_start].altitude:
            cur_end = i
            inslope = True
    return slopes

def match_slopes(se, offset=70):
    Slope = get_model('turan', 'Slope')
    slopes = Slope.objects.filter(start_lon__gt=0)
    for s in slopes:
        start_distance = proj_distance(se.start_lat, se.start_lon, s.start_lat, s.start_lon)
        if start_distance and start_distance < offset:
            end_distance = proj_distance(se.end_lat, se.end_lon, s.end_lat, s.end_lon)
            if end_distance and end_distance < offset:
                print start_distance, end_distance
                if not s.segment:
                    s.segment = se
                    s.save()

@task
def create_simplified_gpx(gpx_path, filename):
    cmd = 'gpsbabel -i gpx -f %s -x duplicate,location -x position,distance=1m -x simplify,crosstrack,error=0.005k -o gpx -F %s' % (\
            gpx_path,
            '/'.join(gpx_path.split('/')[0:-2]) + '/' + filename)
    retcode = call(cmd.split())
    return retcode

@task
def create_svg_from_gpx(gpx_path, filename):
    g = GPX2SVG(gpx_path)
    svg = g.xml
    gpxstore.save(filename, ContentFile(svg))
    return True

@task
def create_gpx_from_details(exercise, callback=None):
#    logger = create_gpx_from_details.get_logger()
#    logger.info('create_gpx_from_details: %s' %exercise.id)

    if exercise.route:
        # Check if the route has .gpx or not.
        # Since we at this point have exercise details
        # we can generate gpx based on that
        if not exercise.route.gpx_file:

            # Check if the details have lon, some parsers doesn't provide position
            if exercise.get_details().filter(lon__gt=0).count() > 0 or exercise.get_details().filter(lon__lt=0).count():
                g = GPXWriter(exercise.get_details().all())
                filename = 'gpx/%s.gpx' %exercise.id

                # tie the created file to the route object
                # also call Save on route to generate start/stop-pos, etc
                exercise.route.gpx_file.save(filename, ContentFile(g.xml), save=True)

                # Save the Route (because of triggers for pos setting and such)
                exercise.route.save()

    if not callback is None:
        subtask(callback).delay(exercise)
#    calculate_best_efforts.delay(exercise)

@task
def merge_sensordata(exercise, callback=None):

    ExerciseDetail = get_model('turan', 'ExerciseDetail')

    for merger in exercise.mergesensorfile_set.all():

        # TODO, merge_types, this is only the merge kind.

        merger.sensor_file.file.seek(0)
        parser = find_parser(merger.sensor_file.name)
        parser.parse_uploaded_file(merger.sensor_file.file)
        for val in parser.entries:
            # Lookup correct detail based on time TODO: more merge strategies
            try:
                ed = ExerciseDetail.objects.get(exercise=exercise, time=val.time)
                for v in ('hr', 'altitude', 'speed', 'cadence', 'position'):
                    want_value = getattr(merger, v)
                    if want_value:
                        if v == 'position':
                            ed.lat = val.lat
                            ed.lon = val.lon
                        else:
                            setattr(ed, v, getattr(val, v))
                ed.save()
            except Exception:
                print "No match: %s" % val.time
                pass # Did not find match, silently continue
    if not callback is None:
        subtask(callback).delay(exercise)
#    create_gpx_from_details.delay(exercise)

def smoothListGaussian(list,degree=5):
    list = [x if x else 0 for x in list] # Change None into 0
    list = [list[0]]*(degree-1) + list + [list[-1]]*degree
    window=degree*2-1
    weight=numpy.array([1.0]*window)
    weightGauss=[]
    for i in range(window):
        i=i-degree+1
        frac=i/float(window)
        gauss=1/(numpy.exp((4*(frac))**2))
        weightGauss.append(gauss)
    weight=numpy.array(weightGauss)*weight
    smoothed=[0.0]*(len(list)-window)
    for i in range(len(smoothed)):
        smoothed[i]=sum(numpy.array(list[i:i+window])*weight)/sum(weight)
    return smoothed

def calculate_ascent_descent_gaussian(details):
    ''' Calculate ascent and descent for an exercise. Use guassian filter to smooth '''

    altvals = []
    for a in details:
        altvals.append(a.altitude)

    altvals = smoothListGaussian(altvals)
    return altvals_to_ascent_descent(altvals)

def altvals_to_ascent_descent(altvals):

    ascent = 0
    descent = 0
    previous = -1

    for a in altvals:
        if previous == -1:
            previous = a

        if a > previous:
            ascent += (a - previous)
        if a < previous:
            descent += (previous - a)

        previous = a
    return round(ascent), round(descent)

def best_x_sec(details, length, altvals, speed=True, power=False):

    best_speed = 0.0
    best_power = 0.0
    best_power = 0.0
    sum_q_power = 0.0
    sum_q_speed = 0.0
    best_start_km_speed = 0.0
    best_start_km_power = 0.0
    best_speed_start_end = 0
    best_power_start_end = 0
    q_speed = deque()
    q_power = deque()
    best_length_speed = 0.0
    best_length_power = 0.0

    if speed:
        q_speed.appendleft(details[0].speed)
    j = 1
    if power and details[j].power:
        q_power.appendleft(details[j].power)
    j = 2
    len_i = len(details)
    for i in xrange(2, len_i):
        #try:
            delta_t = (details[i].time - details[i-1].time).seconds
            if speed:
                # Break if exerciser is on a break as well
                if delta_t < 60:
                    q_speed.appendleft(details[i].speed * delta_t)
                else:
                    q_speed = deque()
                    q_speed.appendleft(details[i].speed)
                delta_t_total = (details[i].time - details[i-len(q_speed)].time).seconds
            if power:
                if delta_t < 60 and details[i].power:
                    q_power.appendleft(details[i].power * delta_t)
                elif delta_t < 60 and not details[i].power:
                    q_power.appendleft(0)
                else:
                    q_power = deque()
                    if details[i].power:
                        q_power.appendleft(details[i].power)
                if not speed:
                    delta_t_total = (details[i].time - details[i-len(q_power)].time).seconds
            if delta_t_total >= length:
                break
            j += 1
        #except Exception as e:
        #    #print "%s %s %s %s %s" % (e, i, j, delta_t, len(q_speed))
        #    #j += 1
        #    continue
    j += 1

    for i in xrange(j, len(details)):

        try:
            if len(q_speed):
                if speed:
                    sum_q_speed_tmp = sum(q_speed)
                    delta_t_total = (details[i].time - details[i-len(q_speed)].time).seconds

                    if delta_t_total != 0 and delta_t_total == length:
                        sum_q_speed = sum_q_speed_tmp / (details[i].time - details[i-len(q_speed)].time).seconds
                    else:
                        # What can one do?
                        sum_q_speed = 0
            if len(q_power):
                if power:
                    sum_q_power_tmp = sum(q_power)
                    delta_t_total_power = (details[i].time - details[i-len(q_power)].time).seconds
                    if delta_t_total_power != 0 and delta_t_total_power == length:
                        sum_q_power = sum_q_power_tmp / delta_t_total_power
                    else:
                        sum_q_power = 0
            if sum_q_speed > best_speed:
                best_speed = sum_q_speed
                best_start_km_speed = details[i-len(q_speed)].distance / 1000
                best_speed_start_end = (i, i-len(q_speed))
                best_length_speed = (details[i].distance) - best_start_km_speed * 1000
            if sum_q_power > best_power:
                best_power = sum_q_power
                best_start_km_power = details[i-len(q_power)].distance / 1000
                best_power_start_end = (i, i-len(q_speed))
                best_length_power = (details[i].distance) - best_start_km_power * 1000

            delta_t = (details[i].time - details[i-1].time).seconds
            if speed:
                if delta_t < 60:
                    q_speed.appendleft(details[i].speed*delta_t)
                else:
                    q_speed = deque()
            if power:
                if delta_t < 60 and details[i].power:
                    q_power.appendleft(details[i].power*delta_t)
                elif delta_t < 60 and not details[i].power:
                    q_power.appendleft(0)
                else:
                    q_power = deque()
            while ((details[i].time - details[i-len(q_speed)].time).seconds) > length:
                q_speed.pop()
            while (power and (details[i].time - details[i-len(q_power)].time).seconds > length):
                q_power.pop()
        except Exception as e:
            #print "something wrong %s, %s, %s, %s" % (e, len(q_speed), i, j)
            #raise
            continue

    if power and speed:
        best_speed_ascent = 0
        best_speed_descent = 0
        best_power_ascent = 0
        best_power_descent = 0
        if best_speed_start_end:
            c, d = best_speed_start_end
            best_speed_ascent, best_speed_descent = altvals_to_ascent_descent(altvals[d:c])
        if best_power_start_end:
            a, b = best_power_start_end
            best_power_ascent, best_power_descent = altvals_to_ascent_descent(altvals[b:a])

        return best_speed, best_start_km_speed, best_length_speed, best_speed_ascent, best_speed_descent, best_power, best_start_km_power, best_length_power, best_power_ascent, best_power_descent
    elif speed and not power:
        best_speed_ascent = 0
        best_speed_descent = 0
        if best_speed_start_end:
            a, b = best_speed_start_end
            best_speed_ascent, best_speed_descent = altvals_to_ascent_descent(altvals[b:a])
        return best_speed, best_start_km_speed, best_length_speed, best_speed_ascent, best_speed_descent
    elif power and not speed:
        best_power_ascent = 0
        best_power_descent = 0
        if best_power_start_end:
            a, b = best_power_start_end
            best_power_ascent, best_power_descent = altvals_to_ascent_descent(altvals[b:a])

        return best_power, best_start_km_power, best_length_power, best_power_ascent, best_power_descent

@task
def calculate_time_in_zones(exercise, callback=None):

    # First: Delete any existing in case of reparse
    exercise.hrzonesummary_set.all().delete()

    HRZoneSummary = get_model('turan', 'HRZoneSummary')

    zones = getzones(exercise)
    for zone, val in zones.items():
        hrz = HRZoneSummary()
        hrz.exercise_id = exercise.id
        hrz.zone = zone
        hrz.duration = val
        hrz.save()

def getzones(exercise):
    ''' Calculate time in different sport zones given trip details '''

    values = exercise.get_details().all()
    max_hr = exercise.user.get_profile().max_hr
    if not max_hr:
        max_hr = 200 # FIXME warning to user etc

    zones = SortedDict({
            0: 0,
            1: 0,
            2: 0,
            3: 0,
            4: 0,
            5: 0,
            6: 0,
        })
    previous_time = False
    if values:
        for d in values:
            if not previous_time:
                previous_time = d.time
                continue
            time = d.time - previous_time
            previous_time = d.time
            if time.seconds > 60:
                continue
            hr_percent = 0
            if d.hr:
                hr_percent = float(d.hr)*100/max_hr
            zone = hr2zone(hr_percent)
            zones[zone] += time.seconds
    else:
        if exercise.duration:
            zones[0] = exercise.duration.seconds

    return zones

@task
def calculate_best_efforts(exercise, effort_range=[5, 10, 30, 60, 240, 300, 600, 1200, 1800, 3600], calc_only_power=True, callback=None):
    ''' Iterate over details for different effort ranges finding best
    speed and power efforts '''

    # First: Delete any existing best efforts
    if not calc_only_power:
        exercise.bestspeedeffort_set.all().delete()
    exercise.bestpowereffort_set.all().delete()
    BestSpeedEffort = get_model('turan', 'BestSpeedEffort')
    BestPowerEffort = get_model('turan', 'BestPowerEffort')

    details = exercise.get_details().all()
    calc_power = exercise.avg_power and not exercise.is_smart_sampled()

    if details:
        if filldistance(details):
            altvals = []
            for a in details:
                altvals.append(a.altitude)

            altvals = smoothListGaussian(altvals)
            for seconds in effort_range:
                if calc_power and not calc_only_power:
                    speed, pos, length, speed_ascent, speed_descent, power, power_pos, power_length, power_ascent, power_descent = best_x_sec(details, seconds, altvals, power=True)
                    if power:
                        be = BestPowerEffort(exercise=exercise, power=power, pos=power_pos, length=power_length, duration=seconds, ascent=power_ascent, descent=power_descent)
                        be.save()
                    if speed:
                        be = BestSpeedEffort(exercise=exercise, speed=speed, pos=pos, length=length, duration=seconds, ascent=speed_ascent, descent=speed_descent)
                        be.save()
                elif not calc_power:
                    speed, pos, length, speed_ascent, speed_descent = best_x_sec(details, seconds, altvals, power=False)
                    if speed:
                        be = BestSpeedEffort(exercise=exercise, speed=speed, pos=pos, length=length, duration=seconds, ascent=speed_ascent, descent=speed_descent)
                        be.save()
                elif calc_power and calc_only_power:
                    power, power_pos, power_length, power_ascent, power_descent = best_x_sec(details, seconds, altvals, power=True, speed=False)
                    if power:
                        be = BestPowerEffort(exercise=exercise, power=power, pos=power_pos, length=power_length, duration=seconds, ascent=power_ascent, descent=power_descent)
                        be.save()
    if not callback is None:
        subtask(callback).delay(exercise)

def normalize_altitude(exercise):
    ''' Normalize altitude, that is, if it's below zero scale every value up.
    Also set max and min altitude on route'''

    altitude_min = exercise.get_details().aggregate(Min('altitude'))['altitude__min']
    altitude_max = exercise.get_details().aggregate(Max('altitude'))['altitude__max']
    # Normalize values
    if altitude_min and altitude_min < 0:
        altitude_min = 0 - altitude_min
        previous_altitude = 0
        for d in exercise.get_details().all():
            if d.altitude == None: # Check for missing altitude values
                d.altitude = previous_altitude
            else:
                d.altitude += altitude_min
            d.save()
            previous_altitude = d.altitude
    # Find min and max and populate route object
    # reget new values after normalize
    altitude_min = exercise.get_details().aggregate(Min('altitude'))['altitude__min']
    altitude_max = exercise.get_details().aggregate(Max('altitude'))['altitude__max']
    r = exercise.route
    if r:
        if not r.min_altitude:
            r.min_altitude = altitude_min
        if not r.max_altitude:
            r.max_altitude = altitude_max
        r.save()

@task
def parse_sensordata(exercise, callback=None):
    ''' The function that takes care of parsing data file from sports equipment from polar or garmin and putting values into the detail-db, and also summarized values for trip. '''

    ExerciseDetail = get_model('turan', 'ExerciseDetail')
    Interval = get_model('turan', 'Interval')

    # Delete any existing Intervals
    exercise.interval_set.all().delete()


    if exercise.get_details().count(): # If the exercise already has details, delete them and reparse
        # Django is super shitty when it comes to deleation. If you want to delete 25k objects, it uses 500 queries to do so.
        # So. We do some RAWness.
        cursor = connection.cursor()

        # Data modifying operation - commit required
        cursor.execute("DELETE FROM turan_exercisedetail WHERE exercise_id = %s", [exercise.id])
        transaction.commit_unless_managed()

    if exercise.slope_set.count(): # If the exercise has slopes, delete them too
        exercise.slope_set.all().delete()


    exercise.sensor_file.file.seek(0)
    parser = find_parser(exercise.sensor_file.name)
    parser.parse_uploaded_file(exercise.sensor_file.file)

    for val in parser.entries:
        detail = ExerciseDetail()
        detail.exercise_id = exercise.id

        # Figure out which values the parser has
        for v in ('distance', 'time', 'hr', 'altitude', 'speed', 'cadence', 'lon', 'lat', 'power', 'temp'):
            if hasattr(val, v):
                #if not types.NoneType == type(val[v]):
                setattr(detail, v, getattr(val, v))
        detail.save()
    # Parse laps/intervals
    for val in parser.laps:
        interval = Interval()
        interval.exercise_id = exercise.id

        # Figure out which values the parser has
        for v in ('start', 'start_time', 'duration', 'distance', 'ascent', 'descent',
                'avg_temp', 'kcal', 'start_lat', 'start_lon', 'end_lat', 'end_lon',
                'avg_hr', 'avg_speed', 'avg_cadence', 'avg_power',
                'max_hr', 'max_speed', 'max_cadence', 'max_power',
                'min_hr', 'min_speed', 'min_cadence', 'min_power',
           ):
            if hasattr(val, v):
                setattr(interval, v, getattr(val, v))
        try:
            interval.save()
        except:
            pass

    exercise.max_hr = parser.max_hr
    exercise.max_speed = parser.max_speed
    exercise.max_cadence = parser.max_cadence
    if hasattr(parser, 'avg_hr'):
        exercise.avg_hr = parser.avg_hr
    exercise.avg_speed = parser.avg_speed
    if hasattr(parser, 'avg_cadence'):
        exercise.avg_cadence = parser.avg_cadence
    if hasattr(parser, 'avg_pedaling_cad'):
        exercise.avg_pedaling_cad = parser.avg_pedaling_cad
    if hasattr(parser, 'duration'):
        exercise.duration = parser.duration

    if parser.kcal_sum: # only some parsers provide kcal
        exercise.kcal = parser.kcal_sum

    if hasattr(parser, 'avg_power'): # only some parsers
        exercise.avg_power = parser.avg_power
        # Generate normalized power
        exercise.normalized_power = power_30s_average(exercise.get_details().all())
    if hasattr(parser, 'max_power'): # only some parsers
        exercise.max_power = parser.max_power
    if hasattr(parser, 'avg_pedaling_power'):
        exercise.avg_pedaling_power = parser.avg_pedaling_power


    if hasattr(parser, 'start_time'):
        if parser.start_time:
            exercise.time = parser.start_time

    if hasattr(parser, 'date'):
        if parser.date:
            exercise.date = parser.date

    if hasattr(parser, 'temperature'):
        if parser.temperature:
            exercise.temperature = parser.temperature

    if hasattr(parser, 'min_temp'):
        if parser.min_temp:
            exercise.min_temperature = parser.min_temp

    if hasattr(parser, 'max_temp'):
        if parser.max_temp:
            exercise.max_temperature = parser.max_temp

    if hasattr(parser, 'comment'): # Polar has this
        if parser.comment: # comment isn't always set
            exercise.comment = parser.comment



    # Normalize altitude, that is, if it's below zero scale every value up
    normalize_altitude(exercise)

    # Auto calculate total ascent and descent
    route = exercise.route
    if route:
        if route.distance:
            # Sum is in meter, but routes like km.
            # use the distance from sensor instead of gps
            if parser.distance_sum and parser.distance_sum/1000 != route.distance:
                route.distance = parser.distance_sum/1000
                route.save()
        elif parser.distance_sum:
            route.distance = parser.distance_sum/1000
            route.save()

        if route.distance:
            ascent, descent = calculate_ascent_descent_gaussian(exercise.get_details().all())
        else:
            ascent = 0
            descent = 0
        # prefer ascent/descent calculated from sensor data over gps
        if route.ascent == 0 or route.descent == 0 \
                or not route.ascent or not route.descent \
                or route.descent != descent or route.ascent != ascent:
            route.ascent = ascent
            route.descent = descent
            route.save()
    exercise.save()

    if not callback is None:
        subtask(callback).delay(exercise)

    # Apply jobs, so we can use this in view
    merge_sensordata(exercise)
    create_gpx_from_details(exercise)
    calculate_best_efforts(exercise)
    calculate_time_in_zones(exercise)
    if hasattr(route, 'ascent') and route.ascent > 0:
        getslopes(exercise.get_details().all(), exercise.user.get_profile().get_weight(exercise.date))


@task
def create_tcx_from_details(event):
    # Check if the details have lon, some parsers doesn't provide position
    if event.get_details().filter(lon__gt=0).filter(lat__gt=0).count() > 0:
        details = event.get_details().all()
        if filldistance(details):
            cadence = 0
            if event.avg_pedaling_cad:
                cadence = event.avg_pedaling_cad
            elif event.avg_cadence:
                cadence = event.avg_cadence
            g = TCXWriter(details, event.route.distance*1000, event.avg_hr, event.max_hr, event.kcal, event.max_speed, event.duration.seconds, details[0].time, cadence)
            filename = '/tmp/%s.tcx' %event.id

            file(filename, 'w').write(g.xml)

def calculate_ascent_descent(event):
    ''' Calculate ascent and descent for an exercise and put on the route.
    Use the 2 previous and the 2 next samples for moving average
    '''


    average_altitudes = []
    details = list(event.get_details().all())
    for i, d in enumerate(details):
        if i > 2 and i < (len(details)-2):
            altitude = d.altitude
            altitude += details[i-1].altitude
            altitude += details[i-2].altitude
            altitude += details[i+1].altitude
            altitude += details[i+2].altitude
            altitude = float(altitude) / 5

        else: # Don't worry about averages at start or end
            altitude = d.altitude
        average_altitudes.append(altitude)


    ascent = 0
    descent = 0
    previous = -1

    for a in average_altitudes:
        if previous == -1:
            previous = a

        if a > previous:
            ascent += (a - previous)
        if a < previous:
            descent += (previous - a)

        previous = a
    return round(ascent), round(descent)

def power_30s_average(details):
    ''' Populate every detail in a detail set with power 30s average and also return the
    normalized power for the exercise'''

    if not details:
        return 0

    #if not details[0].exercise.avg_power:
    #    # Do not generate for exercise without power
    #    return 0

    datasetlen = len(details)

    # TODO implement for non 1 sec sample, for now return blank
    sample_len = (details[datasetlen/2].time - details[(datasetlen/2)-1].time).seconds
    if sample_len > 1:
        return 0

    normalized = 0.0
    fourth = 0.0
    power_avg_count = 0

    #FORCING 1 SEC SAMPLE INTERVAL!
    for i in xrange(0, datasetlen):
        foo = 0.0
        foo_element = 0.0
        for j in xrange(0,30):
            if (i+j-30) > 0 and (i+j-30) < datasetlen:
                delta_t = (details[i+j-30].time - details[i+j-31].time).seconds
                # Break if sample is not 1 sek...
                if delta_t == 1:
                    power = details[i+j-30].power
                    if power:
                        foo += power*delta_t
                        foo_element += 1.0
                else:
                    foo = 0
                    foo_element = 0.0
                    break
        if foo_element:
            poweravg30s = foo/foo_element
            details[i].poweravg30s = poweravg30s
            fourth += pow(poweravg30s, 4)
            power_avg_count += 1

    if not fourth or not power_avg_count:
        return 0
    normalized = int(round(pow((fourth/power_avg_count), (0.25))))
    return normalized

def hr2zone(hr_percent):
    ''' Given a HR percentage return sport zone based on Olympiatoppen zones'''

    zone = 0

    if hr_percent > 97:
        zone = 6
    elif hr_percent > 92:
        zone = 5
    elif hr_percent > 87:
        zone = 4
    elif hr_percent > 82:
        zone = 3
    elif hr_percent > 72:
        zone = 2
    elif hr_percent > 60:
        zone = 1

    return zone
