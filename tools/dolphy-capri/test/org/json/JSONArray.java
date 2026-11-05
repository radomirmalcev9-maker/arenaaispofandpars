package org.json;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
/** Minimal test double for the array literals exercised by the JVM harness. */
public class JSONArray {
    private final List<Object> values = new ArrayList<>();
    public JSONArray(Collection<?> items) { values.addAll(items); }
    public JSONArray(String text) {
        text = text.trim();
        if (!text.startsWith("[") || !text.endsWith("]")) throw new IllegalArgumentException("bad JSON");
        String body = text.substring(1, text.length()-1).trim();
        if (body.isEmpty()) return;
        for (String item : body.split(",")) {
            item = item.trim();
            if (item.startsWith("\"") && item.endsWith("\"")) values.add(item.substring(1,item.length()-1));
            else if ("null".equals(item)) values.add(JSONObject.NULL);
            else if ("true".equals(item) || "false".equals(item)) values.add(Boolean.valueOf(item));
            else values.add(Integer.valueOf(item));
        }
    }
    public int length() { return values.size(); }
    public Object get(int i) { return values.get(i); }
    public String getString(int i) { return (String) values.get(i); }
    public String toString() { return values.toString(); }
}
