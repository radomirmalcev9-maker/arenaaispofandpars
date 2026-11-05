package app.capri.integration;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.lang.reflect.Array;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Capri tool -> Dolphy's existing public plugin bridges. No web server, exported
 * IPC endpoint, or implicit root grant is introduced. Each API invocation
 * requires a visible, per-call confirmation by the person using the phone.
 */
public final class DolphyBridge {
    private static final String DEVICE = "com.droid.dolphy.plugin.bridge.PluginDeviceApis";
    private static final String ANDROID = "com.droid.dolphy.plugin.bridge.PluginAndroidApis";
    private static final String DOLPHY_FUNCTION = "kotlin.jvm.functions.Function1";
    private static final String CAPRI_FUNCTION = "capriembed.kotlin.jvm.functions.Function1";
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final ExecutorService WORKER = Executors.newCachedThreadPool();
    private static volatile Object deviceApi;
    private static volatile Object androidApi;
    private static volatile List<String> routes;
    private static final int INPUT_LIMIT = 8192;
    private static final int OUTPUT_LIMIT = 30000;

    private DolphyBridge() {}

    /** Returns false only for commands which still belong to original Capri. */
    public static boolean intercept(Object call, Object ui, Object callback) {
        boolean claimed = false;
        try {
            String name = String.valueOf(call.getClass().getMethod("getName").invoke(call));
            @SuppressWarnings("unchecked") List<Object> args =
                    (List<Object>) call.getClass().getMethod("getArgs").invoke(call);
            if ("open".equals(name) && !args.isEmpty() && isDolphyName(String.valueOf(args.get(0)))) {
                claimed = true;
                Tabs.openDolphy(null);
                new Completion(callback).post("Opening Dolphy. Use screen to inspect it; access depends on Android permissions.");
                return true;
            }
            if (!"dolphy".equals(name)) return false;
            claimed = true;
            Completion completion = new Completion(callback);
            if (args.isEmpty()) {
                completion.post("Use dolphy|routes, dolphy|open|route, dolphy|list|device|filter, or dolphy|device|method|[arguments].");
                return true;
            }
            String mode = String.valueOf(args.get(0)).toLowerCase(Locale.ROOT);
            if ("routes".equals(mode)) {
                completion.post("Dolphy routes: " + new JSONArray(getRoutes()).toString());
            } else if ("open".equals(mode)) {
                String route = args.size() > 1 ? String.valueOf(args.get(1)) : "";
                if (!route.isEmpty() && !getRoutes().contains(route)) {
                    completion.post("Unknown Dolphy route. Use dolphy|routes first.");
                } else {
                    Tabs.openDolphy(route);
                    completion.post("Opening Dolphy" + (route.isEmpty() ? "" : " route " + route)
                            + ". Use screen and inspect the actual result.");
                }
            } else if ("list".equals(mode)) {
                String scope = args.size() > 1 ? String.valueOf(args.get(1)) : "device";
                String filter = args.size() > 2 ? String.valueOf(args.get(2)) : "";
                completion.post(listMethods(scope, filter));
            } else if ("device".equals(mode) || "android".equals(mode)) {
                if (args.size() < 2) {
                    completion.post("Missing method. Use dolphy|list|" + mode + "|filter.");
                } else {
                    String method = String.valueOf(args.get(1));
                    StringBuilder json = new StringBuilder();
                    for (int i = 2; i < args.size(); i++) {
                        if (i > 2) json.append('|');
                        json.append(args.get(i));
                    }
                    String raw = json.length() == 0 ? "[]" : json.toString();
                    if (raw.length() > INPUT_LIMIT) throw new IllegalArgumentException("Arguments are too long");
                    JSONArray arguments = new JSONArray(raw);
                    MethodAndArgs selected = selectMethod(mode, method, arguments, completion);
                    confirmAndCall(mode, method, raw, selected, completion);
                }
            } else {
                completion.post("Unknown Dolphy command. Use dolphy|routes or dolphy|list|device.");
            }
            return true;
        } catch (Throwable error) {
            if (claimed) new Completion(callback).post("Dolphy request failed: " + message(error));
            // Do not swallow unrelated Capri tools; recognized Dolphy commands
            // must never fall through and execute a different operation.
            return claimed;
        }
    }

    private static boolean isDolphyName(String value) {
        String name = value.trim().toLowerCase(Locale.ROOT);
        return name.equals("dolphy") || name.equals("долфи") || name.equals("долфі");
    }

    private static synchronized Object api(String scope) throws Exception {
        if (!"device".equals(scope) && !"android".equals(scope))
            throw new IllegalArgumentException("Scope must be device or android");
        if (Tabs.context() == null) throw new IllegalStateException("Dolphy is not initialized");
        if ("device".equals(scope)) {
            if (deviceApi == null) deviceApi = Class.forName(DEVICE)
                    .getConstructor(android.content.Context.class, String.class)
                    .newInstance(Tabs.context(), "capri");
            return deviceApi;
        }
        if (androidApi == null) androidApi = Class.forName(ANDROID)
                .getConstructor(android.content.Context.class, String.class)
                .newInstance(Tabs.context(), "capri");
        return androidApi;
    }

    private static List<String> getRoutes() throws Exception {
        if (routes != null) return routes;
        synchronized (DolphyBridge.class) {
            if (routes != null) return routes;
            if (Tabs.context() == null) throw new IllegalStateException("Dolphy is not initialized");
            try (InputStream in = Tabs.context().getAssets().open("capri_dolphy_routes.json")) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buffer = new byte[4096];
                int n;
                while ((n = in.read(buffer)) != -1) {
                    if (out.size() + n > 65536) throw new IllegalStateException("Invalid route list");
                    out.write(buffer, 0, n);
                }
                JSONArray data = new JSONArray(new String(out.toByteArray(), StandardCharsets.UTF_8));
                List<String> loaded = new ArrayList<>();
                for (int i = 0; i < data.length(); i++) loaded.add(data.getString(i));
                routes = Collections.unmodifiableList(loaded);
                return routes;
            }
        }
    }

    private static String listMethods(String scope, String filter) throws Exception {
        Object api = api(scope);
        String needle = filter.toLowerCase(Locale.ROOT);
        List<String> names = new ArrayList<>();
        for (Method method : api.getClass().getMethods()) {
            if (!isPublicApi(method, api.getClass()) || !method.getName().toLowerCase(Locale.ROOT).contains(needle)) continue;
            StringBuilder sig = new StringBuilder(method.getName()).append('(');
            Class<?>[] types = method.getParameterTypes();
            for (int i = 0; i < types.length; i++) {
                if (i > 0) sig.append(',');
                sig.append(types[i].getName().equals(DOLPHY_FUNCTION) ? "callback" : types[i].getSimpleName());
            }
            names.add(sig.append(')').toString());
        }
        Collections.sort(names);
        int total = names.size();
        if (names.size() > 100) names = names.subList(0, 100);
        return "Dolphy " + scope + " methods (" + total + " matches; filter to narrow): " + new JSONArray(names);
    }

    private static boolean isPublicApi(Method method, Class<?> declaring) {
        return method.getDeclaringClass() == declaring && Modifier.isPublic(method.getModifiers())
                && !Modifier.isStatic(method.getModifiers()) && !method.isSynthetic()
                && !method.getName().contains("$");
    }

    private static final class MethodAndArgs {
        final Object receiver;
        final Method method;
        final Object[] values;
        final boolean callback;
        MethodAndArgs(Object receiver, Method method, Object[] values, boolean callback) {
            this.receiver = receiver;
            this.method = method;
            this.values = values;
            this.callback = callback;
        }
    }

    private static MethodAndArgs selectMethod(String scope, String name, JSONArray args, Completion done) throws Exception {
        if (!name.matches("[A-Za-z][A-Za-z0-9_]{0,79}")) throw new IllegalArgumentException("Invalid method name");
        Object instance = api(scope);
        Method[] methods = instance.getClass().getMethods();
        Arrays.sort(methods, new java.util.Comparator<Method>() {
            @Override public int compare(Method a, Method b) { return a.toString().compareTo(b.toString()); }
        });
        for (Method method : methods) {
            if (!method.getName().equals(name) || !isPublicApi(method, instance.getClass())) continue;
            Class<?>[] types = method.getParameterTypes();
            boolean withCallback = types.length > 0 && types[types.length - 1].getName().equals(DOLPHY_FUNCTION);
            if (types.length - (withCallback ? 1 : 0) != args.length()) continue;
            Object[] values = new Object[types.length];
            try {
                for (int i = 0; i < args.length(); i++) values[i] = convert(args.get(i), types[i]);
                if (withCallback) values[types.length - 1] = callback(types[types.length - 1], done);
                return new MethodAndArgs(instance, method, values, withCallback);
            } catch (IllegalArgumentException mismatch) {
                // Try a different overload, if one exists.
            }
        }
        throw new IllegalArgumentException("No supported overload. List its signature; object-only Android parameters may require the Dolphy UI.");
    }

    private static Object convert(Object value, Class<?> type) throws Exception {
        if (value == JSONObject.NULL) {
            if (type.isPrimitive()) throw new IllegalArgumentException("null for primitive");
            return null;
        }
        if (type == String.class && value instanceof String) return value;
        if ((type == boolean.class || type == Boolean.class) && value instanceof Boolean) return value;
        if ((type == int.class || type == Integer.class) && value instanceof Number) {
            long n = ((Number) value).longValue();
            if (n < Integer.MIN_VALUE || n > Integer.MAX_VALUE) throw new IllegalArgumentException("int overflow");
            return (int) n;
        }
        if ((type == long.class || type == Long.class) && value instanceof Number) return ((Number) value).longValue();
        if ((type == float.class || type == Float.class) && value instanceof Number) return ((Number) value).floatValue();
        if ((type == double.class || type == Double.class) && value instanceof Number) return ((Number) value).doubleValue();
        if (type == Uri.class && value instanceof String) return Uri.parse((String) value);
        if (type == File.class && value instanceof String) return new File((String) value);
        if (type == JSONObject.class && value instanceof JSONObject) return value;
        if (type == JSONArray.class && value instanceof JSONArray) return value;
        if (type == byte[].class && value instanceof String) return Base64.decode((String) value, Base64.DEFAULT);
        if (type.isArray() && value instanceof JSONArray) {
            JSONArray source = (JSONArray) value;
            Object result = Array.newInstance(type.getComponentType(), source.length());
            for (int i = 0; i < source.length(); i++) Array.set(result, i, convert(source.get(i), type.getComponentType()));
            return result;
        }
        if (List.class.isAssignableFrom(type) && value instanceof JSONArray) return asList((JSONArray) value);
        if (Map.class.isAssignableFrom(type) && value instanceof JSONObject) return asMap((JSONObject) value);
        throw new IllegalArgumentException("Unsupported argument: " + type.getName());
    }

    private static List<Object> asList(JSONArray json) throws Exception {
        List<Object> result = new ArrayList<>();
        for (int i = 0; i < json.length(); i++) result.add(asJava(json.get(i)));
        return result;
    }
    private static Map<String, Object> asMap(JSONObject json) throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        Iterator<String> keys = json.keys();
        while (keys.hasNext()) { String key = keys.next(); result.put(key, asJava(json.get(key))); }
        return result;
    }
    private static Object asJava(Object value) throws Exception {
        if (value == JSONObject.NULL) return null;
        if (value instanceof JSONArray) return asList((JSONArray) value);
        if (value instanceof JSONObject) return asMap((JSONObject) value);
        return value;
    }

    private static Object callback(Class<?> function, final Completion completion) {
        return Proxy.newProxyInstance(function.getClassLoader(), new Class<?>[]{function}, new InvocationHandler() {
            @Override public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                if ("invoke".equals(method.getName())) {
                    completion.post("Dolphy callback: " + describe(args != null && args.length > 0 ? args[0] : null));
                    Class<?> unit = Class.forName("kotlin.Unit");
                    for (Field field : unit.getFields()) {
                        if (Modifier.isStatic(field.getModifiers()) && field.getType() == unit) return field.get(null);
                    }
                    return null;
                }
                if ("toString".equals(method.getName())) return "DolphyCallback";
                if ("hashCode".equals(method.getName())) return System.identityHashCode(proxy);
                if ("equals".equals(method.getName())) return args != null && args.length == 1 && proxy == args[0];
                return null;
            }
        });
    }

    private static void confirmAndCall(final String scope, final String name, final String raw,
                                       final MethodAndArgs call, final Completion completion) {
        MAIN.post(new Runnable() {
            @Override public void run() {
                Activity activity = Tabs.activity();
                if (activity == null || activity.isFinishing()) {
                    completion.post("Cannot ask for confirmation: open Capri or Dolphy on the phone and try again.");
                    return;
                }
                try {
                    final AtomicBoolean approved = new AtomicBoolean();
                    final AlertDialog dialog = new AlertDialog.Builder(activity)
                            .setTitle("Разрешить действие Capri?")
                            .setMessage("Функция Dolphy: " + scope + "." + name + "\n\nАргументы: " + raw
                                    + "\n\nВыполняйте только для своих устройств и проверьте параметры. "
                                    + "Root и системные разрешения выдаются отдельно.")
                            .setNegativeButton("Отмена", new DialogInterface.OnClickListener() {
                                @Override public void onClick(DialogInterface dialog, int which) { completion.post("Dolphy action cancelled by user."); }
                            })
                            .setPositiveButton("Разрешить", new DialogInterface.OnClickListener() {
                                @Override public void onClick(DialogInterface dialog, int which) {
                                    approved.set(true);
                                    MAIN.postDelayed(new Runnable() {
                                        @Override public void run() {
                                            completion.post("Dolphy did not return within 45 seconds; it may still be running. Check the device before retrying.");
                                        }
                                    }, 45000);
                                    WORKER.execute(new Runnable() {
                                        @Override public void run() { execute(call, completion); }
                                    });
                                }
                            })
                            .setOnCancelListener(new DialogInterface.OnCancelListener() {
                                @Override public void onCancel(DialogInterface dialog) { completion.post("Dolphy action cancelled by user."); }
                            })
                            .setOnDismissListener(new DialogInterface.OnDismissListener() {
                                @Override public void onDismiss(DialogInterface dialog) {
                                    if (!approved.get()) completion.post("Dolphy action cancelled; no confirmation was received.");
                                }
                            }).create();
                    dialog.show();
                    MAIN.postDelayed(new Runnable() {
                        @Override public void run() {
                            if (dialog.isShowing()) {
                                dialog.dismiss();
                                completion.post("Dolphy confirmation timed out without approval.");
                            }
                        }
                    }, 120000);
                } catch (Throwable error) { completion.post("Cannot show Dolphy confirmation: " + message(error)); }
            }
        });
    }

    private static void execute(MethodAndArgs call, final Completion done) {
        try {
            Object result = call.method.invoke(call.receiver, call.values);
            if (!call.callback) {
                done.post("Dolphy result: " + describe(result));
            } else if (Boolean.FALSE.equals(result)) {
                done.post("Dolphy rejected the request (false).");
            }
            // Callback-based operations complete via the callback or the timeout
            // scheduled as soon as the user approves the action.
        } catch (Throwable error) { done.post("Dolphy action failed: " + message(error)); }
    }

    private static String describe(Object value) {
        if (value == null) return "completed (no return value)";
        String text = value instanceof byte[] ? Base64.encodeToString((byte[]) value, Base64.NO_WRAP) : String.valueOf(value);
        return text.length() > OUTPUT_LIMIT ? text.substring(0, OUTPUT_LIMIT) + "… [truncated]" : text;
    }

    private static String message(Throwable error) {
        Throwable cause = error;
        while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
        String text = cause.getMessage();
        return cause.getClass().getSimpleName() + (text == null ? "" : ": " + text);
    }

    /** Calls the relocated Capri Function1 exactly once, on its UI thread. */
    private static final class Completion {
        private final Object callback;
        private final AtomicBoolean settled = new AtomicBoolean();
        Completion(Object callback) { this.callback = callback; }
        void post(final String text) {
            if (!settled.compareAndSet(false, true)) return;
            MAIN.post(new Runnable() {
                @Override public void run() {
                    try {
                        Class.forName(CAPRI_FUNCTION).getMethod("invoke", Object.class).invoke(callback, text);
                    } catch (Throwable ignored) { /* Activity may already be gone. */ }
                }
            });
        }
    }
}
