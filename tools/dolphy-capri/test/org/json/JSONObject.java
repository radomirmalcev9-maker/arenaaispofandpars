package org.json;
import java.util.Collections;
import java.util.Iterator;
public class JSONObject {
    public static final Object NULL = new Object();
    public Iterator<String> keys() { return Collections.<String>emptyList().iterator(); }
    public Object get(String name) { throw new IllegalArgumentException(); }
}
