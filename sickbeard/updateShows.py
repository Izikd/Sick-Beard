# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of Sick Beard.
#
# Sick Beard is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Sick Beard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.



import datetime
import threading
import time
import urllib2
import sqlite3
import gc
import traceback

import sickbeard

from sickbeard import db
from sickbeard.logging import *

from lib.BeautifulSoup import BeautifulStoneSoup
from lib.tvdb_api import tvdb_api, tvdb_exceptions

class UpdateScheduler():

    def __init__(self):
        
        self.lastRun = datetime.datetime.now() # don't run it to start off
        #self.lastRun = datetime.datetime.fromordinal(1) #start it right away
        self.updater = ShowUpdater()
        self.cycleTime = datetime.timedelta(hours=1)
        
        self.thread = None
        self.initThread()
        
        self.abort = False
        
    def initThread(self):
        if self.thread == None:
            self.thread = threading.Thread(None, self.runUpdate, "UPDATE")
        
    def runUpdate(self):
        
        while True:
            
            currentTime = datetime.datetime.now()
            
            if currentTime - self.lastRun > self.cycleTime:
                self.lastRun = currentTime
                try:
                    self.updater.updateShowsFromTVDB()
                except Exception as e:
                    Logger().log("Error encountered while updating shows: " + str(e), ERROR)
                    Logger().log(traceback.format_exc(), DEBUG)
            
            if self.abort:
                self.abort = False
                self.thread = None
                return
            
            time.sleep(1) 
            

class ShowUpdater():

    def __init__(self):
        self._lastTVDB = 0

    def _getUpdatedShows(self, timestamp=None):
        
        if timestamp == None:
            timestamp = self._lastTVDB
        
        if timestamp < 1:
            return (0, None, None)
        
        url = 'http://www.thetvdb.com/api/Updates.php?type=all&time=' + str(timestamp)
        
        try:
            urlObj = urllib2.urlopen(url, timeout=180)
        except IOError as e:
            Logger().log("Unable to retrieve updated shows, assuming everything needs updating: " + str(e), ERROR)
            return (0, None, None)
        
        soup = BeautifulStoneSoup(urlObj)
        
        newTime = int(soup.time.string)
        
        updatedSeries = []
        updatedEpisodes = []
        
        for curSeries in soup.findAll('series'):
            updatedSeries.append(int(curSeries.string))
            
        for curEpisode in soup.findAll('episode'):
            updatedEpisodes.append(int(curEpisode.string))
            
        return (newTime, updatedSeries, updatedEpisodes)

    def _get_lastTVDB(self):
    
        myDB = db.DBConnection()
        myDB.checkDB()
        
        sqlResults = []
        
        Logger().log("Retrieving the last TVDB update time from the DB", DEBUG)
        
        try:
            sql = "SELECT * FROM info"
            Logger().log("SQL: " + sql, DEBUG)
            sqlResults = myDB.connection.execute(sql).fetchall()
        except sqlite3.DatabaseError as e:
            Logger().log("Fatal error executing query '" + sql + "': " + str(e), ERROR)
            raise
    
        if len(sqlResults) == 0:
            lastTVDB = 0
        elif sqlResults[0]["last_tvdb"] == None or sqlResults[0]["last_tvdb"] == "":
            lastTVDB = 0
        else:
            lastTVDB = int(sqlResults[0]["last_tvdb"])
    
        Logger().log("Last TVDB update changed from " + str(self._lastTVDB) + " to " + str(lastTVDB), DEBUG)
        
        self._lastTVDB = lastTVDB
        
        return self._lastTVDB
    
    
    def _set_lastTVDB(self, when):
    
        myDB = db.DBConnection()
        myDB.checkDB()
        
        Logger().log("Setting the last TVDB update in the DB to " + str(int(when)), DEBUG)
        
        try:
            sql = "UPDATE info SET last_tvdb=" + str(int(when))
            Logger().log("SQL: " + sql, DEBUG)
            myDB.connection.execute(sql)
            myDB.connection.commit()
        except sqlite3.DatabaseError as e:
            Logger().log("Fatal error executing query '" + sql + "': " + str(e), ERROR)
            raise

    def _getNewestDBEpisode(self, show):
        
        myDB = db.DBConnection()
        myDB.checkDB()

        sqlResults = []

        try:
            sql = "SELECT * FROM tv_episodes WHERE showid="+str(show.tvdbid)+" ORDER BY airdate DESC LIMIT 1"
            sqlResults = myDB.connection.execute(sql).fetchall()
        except sqlite3.DatabaseError as e:
            Logger().log("Fatal error executing query '" + sql + "': " + str(e), ERROR)
            raise

        if len(sqlResults) == 0:
            return None
        
        sqlResults = sqlResults[0]
        
        if sqlResults["season"] == None or sqlResults["episode"] == None or sqlResults["airdate"] == None:
            return None
    
        Logger().log("Newest DB episode for "+show.name+" was "+str(sqlResults['season'])+"x"+str(sqlResults['episode']), DEBUG)
        
        return (int(sqlResults["season"]), int(sqlResults["episode"]), int(sqlResults["airdate"]))

    def updateShowFromTVDB(self, show, force=False):
        
        if show == None:
            return None
        
        self._get_lastTVDB()
        
        newTime, updatedShows, updatedEpisodes = self._getUpdatedShows()
        Logger().log("Shows that have been updated since " + str(self._lastTVDB) + " are " + str(updatedShows), DEBUG)
        
        t = None
        
        try:
            t = tvdb_api.Tvdb(cache=False, lastTimeout=sickbeard.LAST_TVDB_TIMEOUT)
        except tvdb_exceptions.tvdb_error:
            Logger().log("Can't update from TVDB if we can't connect to it..", ERROR)
           
        doUpdate = updatedShows == None or int(show.tvdbid) in updatedShows or force
            
        if doUpdate:
            
            Logger().log("Updating " + str(show.name) + " (" + str(show.tvdbid) + ")")

            with show.lock:
                show.loadFromTVDB(cache=False)
                show.saveToDB()

            if force:
                Logger().log("Forcing update of all info from TVDB")
                show.loadEpisodesFromTVDB()
                
            else:
                # update each episode that has changed
                epList = sickbeard.getEpList(updatedEpisodes, show.tvdbid)
                Logger().log("Updated episodes for this show are " + str(epList), DEBUG)
                for curEp in epList:
                    Logger().log("Updating episode " + str(curEp.season) + "x" + str(curEp.episode))
                    curEp.loadFromTVDB(int(curEp.season), int(curEp.episode))
                    curEp.saveToDB()
                newestDBEp = self._getNewestDBEpisode(show)
                if t != None and newestDBEp != None:
                    s = t[int(show.tvdbid)]
                    for curEp in s.findNewerEps(newestDBEp[2]):
                        # add the episode
                        newEp = show.getEpisode(int(curEp['seasonnumber']), int(curEp['episodenumber']), True)
                        Logger().log("Added episode "+show.name+" - "+str(newEp.season)+"x"+str(newEp.episode)+" to the DB.")
        
        # now that we've updated the DB from TVDB see if there's anything we can add from TVRage
        with show.lock:
            show.loadLatestFromTVRage()

        # finish up the update
        if doUpdate:
            
            show.writeEpisodeNFOs()
            
            # try keeping ram down
            show.flushEpisodes()
            gc.collect() # try it
            
            Logger().log("Update complete")


    def updateShowsFromTVDB(self):
    
        Logger().log("Beginning update of all shows", DEBUG)

        # check when we last updated
        self._get_lastTVDB()
        
        # get a list of shows that have changed since the last update
        newTime, updatedShows, updatedEpisodes = self._getUpdatedShows()
        Logger().log("Shows that have been updated since " + str(self._lastTVDB) + " are " + str(updatedShows) + " and now it's " + str(newTime), DEBUG)

        if newTime == 0:
            newTime = time.time()

        # if we didn't get a response from TVDB and it's been more than a day since our last update then force it
        forceUpdate = False
        if updatedShows == None:
            if datetime.datetime.now() - datetime.datetime.fromtimestamp(self._lastTVDB) >= datetime.timedelta(hours=24):
                forceUpdate = True
                Logger().log("No response received from TVDB and it's been more than 24 hrs since our last update so we're forcing all shows to update")
            else:
                Logger().log("No response received from TVDB, skipping update for now")
                return

        # check each show to see if it's changed, if so then update it
        for show in sickbeard.showList:
            if forceUpdate or int(show.tvdbid) in updatedShows:
                Logger().log("Updating " + str(show.name) + " (" + str(show.tvdbid) + ")")
                with show.lock:
                    show.loadFromTVDB(cache=False)
                with show.lock:
                    show.saveToDB()

                # update each episode that has changed
                epList = sickbeard.getEpList(updatedEpisodes, show.tvdbid)
                Logger().log("Updated episodes for this show are " + str(epList), DEBUG)
                for curEp in epList:
                    Logger().log("Updating episode " + str(curEp.season) + "x" + str(curEp.episode))
                    curEp.loadFromTVDB(int(curEp.season), int(curEp.episode))
                    curEp.saveToDB()
                
                #show.loadEpisodesFromTVDB(False)

                Logger().log("Update complete")
            else:
                Logger().log("Skipping show " + str(show.name) + ", TVDB says it hasn't changed")

        # update our last update time
        self._set_lastTVDB(newTime)