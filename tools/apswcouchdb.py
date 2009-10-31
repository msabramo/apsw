#

## This code provides a bridge between SQLite and Couchdb. It is
## implemented as a SQLite virtual table.  You just need to import the
## file which will automatically make it available to any new APSW
## connections.  You can also use -init or .read from the APSW Shell
## and it will become available within the shell session.

import apsw
import couchdb
import random
from uuid import uuid4

couchdb

class Source:

    "Called when a table is created"
    def Create(self, db, modulename, dbname, tablename, *args):
        # args[0] must be url of couchdb authentication information.
        # For example http://user:pass@example.com
        # args[1] is db name'

        # sqlite provides the args still quoted etc.  We have to strip
        # them off.

        args=[eval(a.replace("\\", "\\\\")) for a in args]
        
        server=couchdb.Server(args[0])
        cdb=server[args[1]]

        cols=[]
        for c in args[2:]:
            if c!='+':
                cols.append(c)
            else:
                1/0

        # use this for permanent tables
        maptable="%s.%s" % (self._fmt_sql_identifier(dbname),
                            self._fmt_sql_identifier(tablename+"_idmap"))
        # and this for temp
        maptable=self._fmt_sql_identifier(tablename+"_idmap")

        t=Table(db, cdb, cols, maptable)

        sql="create table ignored("+",".join([self._fmt_sql_identifier(c) for c in cols])+")"
        return sql, t

    Connect=Create

    def _fmt_sql_identifier(self, v):
        "Return the identifier quoted in SQL syntax if needed (eg table and column names)"
        if not len(v): # yes sqlite does allow zero length identifiers
            return '""'
        # double quote it unless there are any double quotes in it
        if '"' in v:
            return "[%s]" % (v,)
        return '"%s"' % (v,)


class Table:

    def __init__(self, adb, cdb, cols, maptable):
        # A temporary table that maps between couchdb _id field for
        # each document and the rowid needed to implement a virtual
        # table. _rowid_ is the 64 bit int id autoassigned by SQLIte.
        # We also keep track of the _rev for each document so that
        # updates can be done
        adb.cursor().execute("create temporary table if not exists %s(_id UNIQUE, _rev)" % (maptable,))

        self.adb=adb
        self.cdb=cdb
        self.cols=cols
        self.maptable=maptable
        self.pending_updates={}
        self.rbatch=5000
        self.wbatch=5000

    def Destroy(self):
        self.adb.cursor().execute("drop table if  exists "+self.maptable)

    def BestIndex(self, *args):
        print "bestindex",`args`
        return None

    def Open(self):
        return Cursor(self)

    def Rename(self):
        raise Exception("Rename not supported")

    def UpdateInsertRow(self, rowid, fields):
        if rowid is not None:
            raise Exception("You cannot specify the rowid")
        _id=None
        if "_id" in self.cols:
            _id=fields[self.cols.index("_id")]
        if _id is None:
            # autogenerate
            _id=uuid4().hex
        data=dict(zip(self.cols, fields))
        data["_id"]=_id
        self.pending_updates[_id]=data

        if len(self.pending_updates)>=self.wbatch:
            self.flushpending()
            
        return self.getrowforid(_id)

    def UpdateDeleteRow(self, rowid):
        row=self.adb.cursor().execute("select _id,_rev from "+self.maptable+" where _rowid_=?", (rowid,)).fetchall()
        assert len(row)
        _id=row[0][0]
        _rev=row[0][1]
        d={"_id": _id, "_deleted": True}
        if _rev:
            d["_rev"]=_rev
        self.pending_updates[_id]=d
        if len(self.pending_updates)>=self.wbatch:
            self.flushpending()

    def UpdateChangeRow(self, rowid, newrowid, fields):
        if newrowid!=rowid:
            raise Exception("You cannot change the rowid")
        row=self.adb.cursor().execute("select _id,_rev from "+self.maptable+" where _rowid_=?", (rowid,)).fetchall()
        assert len(row)
        _id,_rev=row[0]

        d=dict(zip(self.cols, fields))
        d["_rev"]=_rev
        d["_id"]=_id
        self.pending_updates[_id]=d
        if len(self.pending_updates)>=self.wbatch:
            self.flushpending()
        
    def getrowforid(self, _id):
        return self.adb.cursor().execute("insert or ignore into "+self.maptable+"(_id) values(?);"
                                        "select _rowid_ from "+self.maptable+" where _id=?",
                                        (_id, _id)).fetchall()[0][0]

    def flushpending(self):
        if not len(self.pending_updates):
            return
        
        p=self.pending_updates.values()
        self.pending_updates={}
        fails=[]
        c=self.adb.cursor()
        for i, (success, docid, rev_or_exc) in enumerate(self.cdb.update(p)):
            if success:
                c.execute("update "+self.maptable+" set _rev=? where _id=?", (rev_or_exc, docid))
            else:
                fails.append("%s: %s\nData: %s" % (docid, rev_or_exc, p[i]))
        if fails:
                raise Exception("Failed to create/update %d documents" % (len(fails),), fails)

    def Begin(self):
        self.flushpending()

    def Sync(self):
        self.flushpending()

    def Commit(self):
        self.flushpending()

    def Rollback(self):
        # Note that we probably already committed stuff anyway so we
        # can't do a true rollback
        self.pending_updates={}

class Cursor:
    def __init__(self, table):
        self.t=table

    def Filter(self, *args):
        # back to begining - we flush any outstanding changes so that we don't have to
        # merge pending updates with server data
        self.t.flushpending()
        self.query=Query(self.t.cdb, self.t.cols, batch=self.t.rbatch)

    def Eof(self):
        # Eof is called before next so we do all the work in eof
        r=self.query.eof()
        if not r:
            self._id, self._rev, self._values = self.query.current()
            self.t.adb.cursor().execute("update "+self.t.maptable+" set _rev=? where _rowid_=?", (self._rev, self.Rowid()))
        return r

    def Rowid(self):
        return self.t.getrowforid(self._id)

    def Column(self, which):
        if which<0:
            return self.Rowid()
        return self._values[which]

    def Next(self):
        pass

    def Close(self):
        pass

class Query:
    """Encapsulates a couchdb query dealing with batching and EOF testing"""
    def __init__(self, cdb, cols, query=None, batch=3):
        self.cdb=cdb
        self.mapfn='''
        function(doc) {
          emit(null, [doc._rev, %s]);
        }''' % (",".join(["doc['%s']===undefined?null:doc['%s']" % (c,c) for c in cols]),)
        self.iter=iter(cdb.query(self.mapfn, limit=batch))
        self.returned=0
        self.batch=batch

    def eof(self):
        while True:
            for self.curval in self.iter:
                self.returned+=1
                return False

            if not self.returned:
                # iterator returned no rows so we are at the end
                self.curval=None
                return True

            # setup next batch
            self.iter=iter(self.cdb.query(self.mapfn, limit=self.batch, skip=1, startkey=None, startkey_docid=self.curval["id"]))
            self.returned=0

    def current(self):
        return self.curval["id"], self.curval["value"][0], self.curval["value"][1:]




# register if invoked from shell
thesource=Source()
def register(db, thesource=thesource):
    db.createmodule("couchdb", thesource)

if 'shell' in locals() and hasattr(shell, "db") and isinstance(shell.db, apsw.Connection):
    register(shell.db)

apsw.connection_hooks.append(register)
    
del thesource
del register
