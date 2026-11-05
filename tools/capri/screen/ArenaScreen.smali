.class public final Lapp/capri/ArenaScreen;
.super Ljava/lang/Object;
.source "ArenaScreen.java"


# annotations
.annotation system Ldalvik/annotation/MemberClasses;
    value = {
        Lapp/capri/ArenaScreen$Api30;,
        Lapp/capri/ArenaScreen$Pending;
    }
.end annotation


# static fields
.field private static final JPEG_LIMIT:I = 0x200000

.field private static final LONG_EDGE:I = 0x780

.field private static final MAIN:Landroid/os/Handler;

.field private static final MARKER:Ljava/util/regex/Pattern;

.field private static final PENDING:Ljava/util/Map;
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "Ljava/util/Map<",
            "Ljava/lang/String;",
            "Lapp/capri/ArenaScreen$Pending;",
            ">;"
        }
    .end annotation
.end field

.field private static final TTL_MS:J = 0x1d4c0L

.field private static final WORKER:Ljava/util/concurrent/ExecutorService;


# direct methods
.method static constructor <clinit>()V
    .locals 2

    .line 42
    new-instance v0, Landroid/os/Handler;

    invoke-static {}, Landroid/os/Looper;->getMainLooper()Landroid/os/Looper;

    move-result-object v1

    invoke-direct {v0, v1}, Landroid/os/Handler;-><init>(Landroid/os/Looper;)V

    sput-object v0, Lapp/capri/ArenaScreen;->MAIN:Landroid/os/Handler;

    .line 43
    invoke-static {}, Ljava/util/concurrent/Executors;->newSingleThreadExecutor()Ljava/util/concurrent/ExecutorService;

    move-result-object v0

    sput-object v0, Lapp/capri/ArenaScreen;->WORKER:Ljava/util/concurrent/ExecutorService;

    .line 44
    const-string v0, "\\[\\[CAPRI_SCREEN_[0-9a-f]{32}\\]\\]"

    invoke-static {v0}, Ljava/util/regex/Pattern;->compile(Ljava/lang/String;)Ljava/util/regex/Pattern;

    move-result-object v0

    sput-object v0, Lapp/capri/ArenaScreen;->MARKER:Ljava/util/regex/Pattern;

    .line 45
    new-instance v0, Ljava/util/LinkedHashMap;

    invoke-direct {v0}, Ljava/util/LinkedHashMap;-><init>()V

    sput-object v0, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    .line 48
    return-void
.end method

.method private constructor <init>()V
    .locals 0

    .line 50
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method static synthetic access$0(Landroid/graphics/Bitmap;Ljava/util/ArrayList;II)V
    .locals 0

    .line 184
    invoke-static {p0, p1, p2, p3}, Lapp/capri/ArenaScreen;->annotate(Landroid/graphics/Bitmap;Ljava/util/ArrayList;II)V

    return-void
.end method

.method static synthetic access$1(Landroid/graphics/Bitmap;)[B
    .locals 0

    .line 207
    invoke-static {p0}, Lapp/capri/ArenaScreen;->compress(Landroid/graphics/Bitmap;)[B

    move-result-object p0

    return-object p0
.end method

.method static synthetic access$2([B)Ljava/lang/String;
    .locals 0

    .line 219
    invoke-static {p0}, Lapp/capri/ArenaScreen;->remember([B)Ljava/lang/String;

    move-result-object p0

    return-object p0
.end method

.method static synthetic access$3()Landroid/os/Handler;
    .locals 1

    .line 42
    sget-object v0, Lapp/capri/ArenaScreen;->MAIN:Landroid/os/Handler;

    return-object v0
.end method

.method static synthetic access$4(Lkotlin/jvm/functions/Function1;Ljava/lang/String;Ljava/lang/String;)V
    .locals 0

    .line 95
    invoke-static {p0, p1, p2}, Lapp/capri/ArenaScreen;->reportFailure(Lkotlin/jvm/functions/Function1;Ljava/lang/String;Ljava/lang/String;)V

    return-void
.end method

.method static synthetic access$5()Ljava/util/concurrent/ExecutorService;
    .locals 1

    .line 43
    sget-object v0, Lapp/capri/ArenaScreen;->WORKER:Ljava/util/concurrent/ExecutorService;

    return-object v0
.end method

.method static synthetic access$6(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V
    .locals 0

    .line 73
    invoke-static {p0, p1, p2}, Lapp/capri/ArenaScreen;->begin(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V

    return-void
.end method

.method static synthetic access$7()Ljava/util/Map;
    .locals 1

    .line 45
    sget-object v0, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    return-object v0
.end method

.method private static annotate(Landroid/graphics/Bitmap;Ljava/util/ArrayList;II)V
    .locals 20
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "(",
            "Landroid/graphics/Bitmap;",
            "Ljava/util/ArrayList<",
            "Landroid/graphics/Rect;",
            ">;II)V"
        }
    .end annotation

    .line 185
    move/from16 v0, p2

    move/from16 v1, p3

    new-instance v10, Landroid/graphics/Canvas;

    move-object/from16 v11, p0

    invoke-direct {v10, v11}, Landroid/graphics/Canvas;-><init>(Landroid/graphics/Bitmap;)V

    .line 186
    new-instance v12, Landroid/graphics/Paint;

    const/4 v2, 0x1

    invoke-direct {v12, v2}, Landroid/graphics/Paint;-><init>(I)V

    .line 187
    invoke-virtual/range {p0 .. p0}, Landroid/graphics/Bitmap;->getWidth()I

    move-result v2

    int-to-float v2, v2

    int-to-float v3, v0

    div-float v13, v2, v3

    .line 188
    invoke-virtual/range {p0 .. p0}, Landroid/graphics/Bitmap;->getHeight()I

    move-result v2

    int-to-float v2, v2

    int-to-float v3, v1

    div-float v14, v2, v3

    .line 189
    invoke-virtual/range {p0 .. p0}, Landroid/graphics/Bitmap;->getWidth()I

    move-result v2

    invoke-virtual/range {p0 .. p0}, Landroid/graphics/Bitmap;->getHeight()I

    move-result v3

    invoke-static {v2, v3}, Ljava/lang/Math;->min(II)I

    move-result v2

    int-to-float v2, v2

    const v3, 0x3c9374bc    # 0.018f

    mul-float/2addr v2, v3

    const/high16 v3, 0x41600000    # 14.0f

    invoke-static {v3, v2}, Ljava/lang/Math;->max(FF)F

    move-result v15

    .line 190
    invoke-virtual {v12, v15}, Landroid/graphics/Paint;->setTextSize(F)V

    .line 191
    sget-object v2, Landroid/graphics/Typeface;->DEFAULT_BOLD:Landroid/graphics/Typeface;

    invoke-virtual {v12, v2}, Landroid/graphics/Paint;->setTypeface(Landroid/graphics/Typeface;)Landroid/graphics/Typeface;

    .line 192
    const/4 v2, 0x0

    move v9, v2

    :goto_0
    invoke-virtual/range {p1 .. p1}, Ljava/util/ArrayList;->size()I

    move-result v2

    if-lt v9, v2, :cond_0

    .line 205
    return-void

    .line 193
    :cond_0
    move-object/from16 v8, p1

    invoke-virtual {v8, v9}, Ljava/util/ArrayList;->get(I)Ljava/lang/Object;

    move-result-object v2

    check-cast v2, Landroid/graphics/Rect;

    .line 194
    invoke-virtual {v2}, Landroid/graphics/Rect;->isEmpty()Z

    move-result v3

    if-nez v3, :cond_2

    iget v3, v2, Landroid/graphics/Rect;->right:I

    if-lez v3, :cond_2

    iget v3, v2, Landroid/graphics/Rect;->bottom:I

    if-lez v3, :cond_2

    iget v3, v2, Landroid/graphics/Rect;->left:I

    if-ge v3, v0, :cond_2

    iget v3, v2, Landroid/graphics/Rect;->top:I

    if-lt v3, v1, :cond_1

    move/from16 v18, v9

    goto :goto_1

    .line 195
    :cond_1
    add-int/lit8 v3, v9, 0x1

    invoke-static {v3}, Ljava/lang/Integer;->toString(I)Ljava/lang/String;

    move-result-object v7

    .line 196
    invoke-virtual {v12, v7}, Landroid/graphics/Paint;->measureText(Ljava/lang/String;)F

    move-result v3

    const v4, 0x3f0ccccd    # 0.55f

    mul-float/2addr v4, v15

    add-float/2addr v3, v4

    .line 197
    const v4, 0x3fa66666    # 1.3f

    mul-float/2addr v4, v15

    .line 198
    invoke-virtual/range {p0 .. p0}, Landroid/graphics/Bitmap;->getWidth()I

    move-result v5

    int-to-float v5, v5

    sub-float/2addr v5, v3

    iget v6, v2, Landroid/graphics/Rect;->left:I

    int-to-float v6, v6

    mul-float/2addr v6, v13

    invoke-static {v5, v6}, Ljava/lang/Math;->min(FF)F

    move-result v5

    const/4 v6, 0x0

    invoke-static {v6, v5}, Ljava/lang/Math;->max(FF)F

    move-result v16

    .line 199
    invoke-virtual/range {p0 .. p0}, Landroid/graphics/Bitmap;->getHeight()I

    move-result v5

    int-to-float v5, v5

    sub-float/2addr v5, v4

    iget v2, v2, Landroid/graphics/Rect;->top:I

    int-to-float v2, v2

    mul-float/2addr v2, v14

    invoke-static {v5, v2}, Ljava/lang/Math;->min(FF)F

    move-result v2

    invoke-static {v6, v2}, Ljava/lang/Math;->max(FF)F

    move-result v17

    .line 200
    const/16 v2, 0x14

    invoke-static {v2, v2, v2}, Landroid/graphics/Color;->rgb(III)I

    move-result v2

    invoke-virtual {v12, v2}, Landroid/graphics/Paint;->setColor(I)V

    .line 201
    add-float v5, v16, v3

    add-float v6, v17, v4

    const/high16 v18, 0x40800000    # 4.0f

    const/high16 v19, 0x40800000    # 4.0f

    move-object v2, v10

    move/from16 v3, v16

    move/from16 v4, v17

    move-object v0, v7

    move/from16 v7, v18

    move/from16 v8, v19

    move/from16 v18, v9

    move-object v9, v12

    invoke-virtual/range {v2 .. v9}, Landroid/graphics/Canvas;->drawRoundRect(FFFFFFLandroid/graphics/Paint;)V

    .line 202
    const/16 v2, -0x100

    invoke-virtual {v12, v2}, Landroid/graphics/Paint;->setColor(I)V

    .line 203
    const/high16 v2, 0x3e800000    # 0.25f

    mul-float/2addr v2, v15

    add-float v2, v16, v2

    add-float v3, v17, v15

    invoke-virtual {v10, v0, v2, v3, v12}, Landroid/graphics/Canvas;->drawText(Ljava/lang/String;FFLandroid/graphics/Paint;)V

    goto :goto_1

    .line 194
    :cond_2
    move/from16 v18, v9

    .line 192
    :goto_1
    add-int/lit8 v9, v18, 0x1

    move/from16 v0, p2

    goto/16 :goto_0
.end method

.method private static begin(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V
    .locals 4

    .line 74
    sget v0, Landroid/os/Build$VERSION;->SDK_INT:I

    const/16 v1, 0x1e

    if-ge v0, v1, :cond_0

    .line 75
    const-string p0, "Screenshot unavailable: Android 11 or newer is required."

    invoke-static {p2, p1, p0}, Lapp/capri/ArenaScreen;->reportFailure(Lkotlin/jvm/functions/Function1;Ljava/lang/String;Ljava/lang/String;)V

    .line 76
    return-void

    .line 81
    :cond_0
    :try_start_0
    invoke-virtual {p0}, Lapp/capri/TypeWatch;->arenaMarks()Ljava/util/ArrayList;

    move-result-object v0

    invoke-virtual {v0}, Ljava/util/ArrayList;->clear()V

    .line 82
    invoke-virtual {p0}, Lapp/capri/TypeWatch;->look()Ljava/lang/String;

    .line 83
    new-instance v0, Ljava/util/ArrayList;

    invoke-direct {v0}, Ljava/util/ArrayList;-><init>()V

    .line 84
    invoke-virtual {p0}, Lapp/capri/TypeWatch;->arenaMarks()Ljava/util/ArrayList;

    move-result-object v1

    invoke-virtual {v1}, Ljava/util/ArrayList;->iterator()Ljava/util/Iterator;

    move-result-object v1

    :goto_0
    invoke-interface {v1}, Ljava/util/Iterator;->hasNext()Z

    move-result v2

    if-nez v2, :cond_1

    .line 89
    invoke-static {p0, p1, p2, v0}, Lapp/capri/ArenaScreen$Api30;->request(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;Ljava/util/ArrayList;)V

    .line 90
    goto :goto_1

    .line 84
    :cond_1
    invoke-interface {v1}, Ljava/util/Iterator;->next()Ljava/lang/Object;

    move-result-object v2

    .line 85
    new-instance v3, Landroid/graphics/Rect;

    invoke-direct {v3}, Landroid/graphics/Rect;-><init>()V

    .line 86
    check-cast v2, Landroid/view/accessibility/AccessibilityNodeInfo;

    invoke-virtual {v2, v3}, Landroid/view/accessibility/AccessibilityNodeInfo;->getBoundsInScreen(Landroid/graphics/Rect;)V

    .line 87
    invoke-virtual {v0, v3}, Ljava/util/ArrayList;->add(Ljava/lang/Object;)Z
    :try_end_0
    .catchall {:try_start_0 .. :try_end_0} :catchall_0

    goto :goto_0

    .line 90
    :catchall_0
    move-exception p0

    .line 91
    const-string p0, "Screenshot unavailable: accessibility capture failed."

    invoke-static {p2, p1, p0}, Lapp/capri/ArenaScreen;->reportFailure(Lkotlin/jvm/functions/Function1;Ljava/lang/String;Ljava/lang/String;)V

    .line 93
    :goto_1
    return-void
.end method

.method private static compress(Landroid/graphics/Bitmap;)[B
    .locals 6

    .line 208
    const/4 v0, 0x4

    new-array v0, v0, [I

    fill-array-data v0, :array_0

    array-length v1, v0

    const/4 v2, 0x0

    :goto_0
    if-ge v2, v1, :cond_2

    aget v3, v0, v2

    .line 209
    new-instance v4, Ljava/io/ByteArrayOutputStream;

    invoke-direct {v4}, Ljava/io/ByteArrayOutputStream;-><init>()V

    .line 210
    sget-object v5, Landroid/graphics/Bitmap$CompressFormat;->JPEG:Landroid/graphics/Bitmap$CompressFormat;

    invoke-virtual {p0, v5, v3, v4}, Landroid/graphics/Bitmap;->compress(Landroid/graphics/Bitmap$CompressFormat;ILjava/io/OutputStream;)Z

    move-result v3

    if-eqz v3, :cond_1

    .line 213
    invoke-virtual {v4}, Ljava/io/ByteArrayOutputStream;->toByteArray()[B

    move-result-object v3

    .line 214
    array-length v4, v3

    if-lez v4, :cond_0

    array-length v4, v3

    const/high16 v5, 0x200000

    if-gt v4, v5, :cond_0

    return-object v3

    .line 208
    :cond_0
    add-int/lit8 v2, v2, 0x1

    goto :goto_0

    .line 211
    :cond_1
    new-instance p0, Ljava/lang/IllegalStateException;

    const-string v0, "JPEG encoding failed"

    invoke-direct {p0, v0}, Ljava/lang/IllegalStateException;-><init>(Ljava/lang/String;)V

    throw p0

    .line 216
    :cond_2
    new-instance p0, Ljava/lang/IllegalStateException;

    const-string v0, "screenshot too large"

    invoke-direct {p0, v0}, Ljava/lang/IllegalStateException;-><init>(Ljava/lang/String;)V

    throw p0

    nop

    :array_0
    .array-data 4
        0x55
        0x46
        0x37
        0x28
    .end array-data
.end method

.method public static configure(Lapp/capri/HandUi;Lapp/capri/ModelEntry;)V
    .locals 1

    .line 63
    if-eqz p1, :cond_0

    invoke-virtual {p1}, Lapp/capri/ModelEntry;->getCloud()Ljava/lang/String;

    move-result-object p1

    const-string v0, "arena"

    invoke-virtual {v0, p1}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result p1

    if-eqz p1, :cond_0

    const/4 p1, 0x1

    goto :goto_0

    :cond_0
    const/4 p1, 0x0

    :goto_0
    invoke-virtual {p0, p1}, Lapp/capri/HandUi;->setArenaScreenshot(Z)V

    .line 64
    return-void
.end method

.method public static inject(Lapp/capri/Provider;Lorg/json/JSONArray;)V
    .locals 10
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Lorg/json/JSONException;
        }
    .end annotation

    .line 253
    invoke-virtual {p0}, Lapp/capri/Provider;->getId()Ljava/lang/String;

    move-result-object p0

    const-string v0, "arena"

    invoke-virtual {v0, p0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result p0

    if-nez p0, :cond_0

    return-void

    .line 254
    :cond_0
    const/4 p0, 0x0

    move v0, p0

    :goto_0
    invoke-virtual {p1}, Lorg/json/JSONArray;->length()I

    move-result v1

    if-lt v0, v1, :cond_1

    .line 290
    return-void

    .line 255
    :cond_1
    invoke-virtual {p1, v0}, Lorg/json/JSONArray;->optJSONObject(I)Lorg/json/JSONObject;

    move-result-object v1

    .line 256
    if-eqz v1, :cond_a

    const-string v2, "role"

    invoke-virtual {v1, v2}, Lorg/json/JSONObject;->optString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v2

    const-string v3, "user"

    invoke-virtual {v3, v2}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v2

    if-nez v2, :cond_2

    goto/16 :goto_4

    .line 257
    :cond_2
    const-string v2, "content"

    invoke-virtual {v1, v2}, Lorg/json/JSONObject;->opt(Ljava/lang/String;)Ljava/lang/Object;

    move-result-object v3

    .line 258
    instance-of v4, v3, Ljava/lang/String;

    if-nez v4, :cond_3

    goto/16 :goto_4

    .line 259
    :cond_3
    check-cast v3, Ljava/lang/String;

    .line 260
    sget-object v4, Lapp/capri/ArenaScreen;->MARKER:Ljava/util/regex/Pattern;

    invoke-virtual {v4, v3}, Ljava/util/regex/Pattern;->matcher(Ljava/lang/CharSequence;)Ljava/util/regex/Matcher;

    move-result-object v3

    .line 261
    new-instance v4, Ljava/lang/StringBuffer;

    invoke-direct {v4}, Ljava/lang/StringBuffer;-><init>()V

    .line 262
    new-instance v5, Ljava/util/ArrayList;

    invoke-direct {v5}, Ljava/util/ArrayList;-><init>()V

    .line 263
    nop

    .line 264
    move v6, p0

    :goto_1
    invoke-virtual {v3}, Ljava/util/regex/Matcher;->find()Z

    move-result v7

    if-nez v7, :cond_7

    .line 273
    if-nez v6, :cond_4

    goto/16 :goto_4

    .line 274
    :cond_4
    invoke-virtual {v3, v4}, Ljava/util/regex/Matcher;->appendTail(Ljava/lang/StringBuffer;)Ljava/lang/StringBuffer;

    .line 275
    invoke-virtual {v5}, Ljava/util/ArrayList;->isEmpty()Z

    move-result v3

    if-eqz v3, :cond_5

    .line 276
    invoke-virtual {v4}, Ljava/lang/StringBuffer;->toString()Ljava/lang/String;

    move-result-object v3

    invoke-virtual {v1, v2, v3}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    .line 277
    goto/16 :goto_4

    .line 279
    :cond_5
    new-instance v6, Lorg/json/JSONArray;

    invoke-direct {v6}, Lorg/json/JSONArray;-><init>()V

    .line 280
    new-instance v3, Lorg/json/JSONObject;

    invoke-direct {v3}, Lorg/json/JSONObject;-><init>()V

    const-string v7, "text"

    const-string v8, "type"

    invoke-virtual {v3, v8, v7}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    move-result-object v3

    invoke-virtual {v4}, Ljava/lang/StringBuffer;->toString()Ljava/lang/String;

    move-result-object v4

    invoke-virtual {v3, v7, v4}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    move-result-object v3

    invoke-virtual {v6, v3}, Lorg/json/JSONArray;->put(Ljava/lang/Object;)Lorg/json/JSONArray;

    .line 281
    invoke-virtual {v5}, Ljava/util/ArrayList;->iterator()Ljava/util/Iterator;

    move-result-object v7

    :goto_2
    invoke-interface {v7}, Ljava/util/Iterator;->hasNext()Z

    move-result v3

    if-nez v3, :cond_6

    .line 288
    invoke-virtual {v1, v2, v6}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    goto :goto_4

    .line 281
    :cond_6
    invoke-interface {v7}, Ljava/util/Iterator;->next()Ljava/lang/Object;

    move-result-object v3

    check-cast v3, Lapp/capri/ArenaScreen$Pending;

    .line 282
    new-instance v4, Lorg/json/JSONObject;

    invoke-direct {v4}, Lorg/json/JSONObject;-><init>()V

    .line 283
    const-string v5, "base64"

    invoke-virtual {v4, v8, v5}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    move-result-object v4

    .line 284
    const-string v5, "media_type"

    const-string v9, "image/jpeg"

    invoke-virtual {v4, v5, v9}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    move-result-object v4

    .line 285
    iget-object v3, v3, Lapp/capri/ArenaScreen$Pending;->jpeg:[B

    const/4 v5, 0x2

    invoke-static {v3, v5}, Landroid/util/Base64;->encodeToString([BI)Ljava/lang/String;

    move-result-object v3

    const-string v5, "data"

    invoke-virtual {v4, v5, v3}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    move-result-object v3

    .line 282
    nop

    .line 286
    new-instance v4, Lorg/json/JSONObject;

    invoke-direct {v4}, Lorg/json/JSONObject;-><init>()V

    const-string v5, "image"

    invoke-virtual {v4, v8, v5}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    move-result-object v4

    const-string v5, "source"

    invoke-virtual {v4, v5, v3}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    move-result-object v3

    invoke-virtual {v6, v3}, Lorg/json/JSONArray;->put(Ljava/lang/Object;)Lorg/json/JSONArray;

    goto :goto_2

    .line 265
    :cond_7
    nop

    .line 266
    invoke-virtual {v3}, Ljava/util/regex/Matcher;->group()Ljava/lang/String;

    move-result-object v6

    invoke-static {v6}, Lapp/capri/ArenaScreen;->take(Ljava/lang/String;)Lapp/capri/ArenaScreen$Pending;

    move-result-object v6

    .line 267
    if-nez v6, :cond_8

    .line 268
    const-string v7, "[Screenshot expired or unavailable; use screen again.]"

    goto :goto_3

    .line 269
    :cond_8
    const-string v7, "[Screenshot attached.]"

    .line 267
    :goto_3
    nop

    .line 270
    if-eqz v6, :cond_9

    invoke-virtual {v5, v6}, Ljava/util/ArrayList;->add(Ljava/lang/Object;)Z

    .line 271
    :cond_9
    invoke-static {v7}, Ljava/util/regex/Matcher;->quoteReplacement(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v6

    invoke-virtual {v3, v4, v6}, Ljava/util/regex/Matcher;->appendReplacement(Ljava/lang/StringBuffer;Ljava/lang/String;)Ljava/util/regex/Matcher;

    const/4 v6, 0x1

    goto/16 :goto_1

    .line 254
    :cond_a
    :goto_4
    add-int/lit8 v0, v0, 0x1

    goto/16 :goto_0
.end method

.method private static prune()V
    .locals 5

    .line 239
    invoke-static {}, Landroid/os/SystemClock;->elapsedRealtime()J

    move-result-wide v0

    .line 240
    sget-object v2, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    invoke-interface {v2}, Ljava/util/Map;->entrySet()Ljava/util/Set;

    move-result-object v2

    invoke-interface {v2}, Ljava/util/Set;->iterator()Ljava/util/Iterator;

    move-result-object v2

    .line 241
    nop

    :cond_0
    :goto_0
    invoke-interface {v2}, Ljava/util/Iterator;->hasNext()Z

    move-result v3

    if-nez v3, :cond_1

    .line 242
    return-void

    .line 241
    :cond_1
    invoke-interface {v2}, Ljava/util/Iterator;->next()Ljava/lang/Object;

    move-result-object v3

    check-cast v3, Ljava/util/Map$Entry;

    invoke-interface {v3}, Ljava/util/Map$Entry;->getValue()Ljava/lang/Object;

    move-result-object v3

    check-cast v3, Lapp/capri/ArenaScreen$Pending;

    iget-wide v3, v3, Lapp/capri/ArenaScreen$Pending;->expires:J

    cmp-long v3, v3, v0

    if-gtz v3, :cond_0

    invoke-interface {v2}, Ljava/util/Iterator;->remove()V

    goto :goto_0
.end method

.method private static remember([B)Ljava/lang/String;
    .locals 4

    .line 220
    new-instance v0, Ljava/lang/StringBuilder;

    const-string v1, "[[CAPRI_SCREEN_"

    invoke-direct {v0, v1}, Ljava/lang/StringBuilder;-><init>(Ljava/lang/String;)V

    invoke-static {}, Ljava/util/UUID;->randomUUID()Ljava/util/UUID;

    move-result-object v1

    invoke-virtual {v1}, Ljava/util/UUID;->toString()Ljava/lang/String;

    move-result-object v1

    const-string v2, "-"

    const-string v3, ""

    invoke-virtual {v1, v2, v3}, Ljava/lang/String;->replace(Ljava/lang/CharSequence;Ljava/lang/CharSequence;)Ljava/lang/String;

    move-result-object v1

    invoke-virtual {v0, v1}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    const-string v1, "]]"

    invoke-virtual {v0, v1}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    invoke-virtual {v0}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v0

    .line 221
    sget-object v1, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    monitor-enter v1

    .line 222
    :try_start_0
    invoke-static {}, Lapp/capri/ArenaScreen;->prune()V

    .line 223
    nop

    :goto_0
    sget-object v2, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    invoke-interface {v2}, Ljava/util/Map;->size()I

    move-result v2

    const/4 v3, 0x2

    if-ge v2, v3, :cond_0

    .line 228
    sget-object v2, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    new-instance v3, Lapp/capri/ArenaScreen$Pending;

    invoke-direct {v3, p0}, Lapp/capri/ArenaScreen$Pending;-><init>([B)V

    invoke-interface {v2, v0, v3}, Ljava/util/Map;->put(Ljava/lang/Object;Ljava/lang/Object;)Ljava/lang/Object;

    .line 221
    monitor-exit v1
    :try_end_0
    .catchall {:try_start_0 .. :try_end_0} :catchall_0

    .line 230
    sget-object p0, Lapp/capri/ArenaScreen;->MAIN:Landroid/os/Handler;

    new-instance v1, Lapp/capri/ArenaScreen$2;

    invoke-direct {v1, v0}, Lapp/capri/ArenaScreen$2;-><init>(Ljava/lang/String;)V

    .line 234
    const-wide/32 v2, 0x1d4c0

    .line 230
    invoke-virtual {p0, v1, v2, v3}, Landroid/os/Handler;->postDelayed(Ljava/lang/Runnable;J)Z

    .line 235
    return-object v0

    .line 224
    :cond_0
    :try_start_1
    sget-object v2, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    invoke-interface {v2}, Ljava/util/Map;->keySet()Ljava/util/Set;

    move-result-object v2

    invoke-interface {v2}, Ljava/util/Set;->iterator()Ljava/util/Iterator;

    move-result-object v2

    .line 225
    invoke-interface {v2}, Ljava/util/Iterator;->next()Ljava/lang/Object;

    .line 226
    invoke-interface {v2}, Ljava/util/Iterator;->remove()V

    goto :goto_0

    .line 221
    :catchall_0
    move-exception p0

    monitor-exit v1
    :try_end_1
    .catchall {:try_start_1 .. :try_end_1} :catchall_0

    throw p0
.end method

.method private static reportFailure(Lkotlin/jvm/functions/Function1;Ljava/lang/String;Ljava/lang/String;)V
    .locals 1

    .line 97
    new-instance v0, Ljava/lang/StringBuilder;

    invoke-static {p1}, Ljava/lang/String;->valueOf(Ljava/lang/Object;)Ljava/lang/String;

    move-result-object p1

    invoke-direct {v0, p1}, Ljava/lang/StringBuilder;-><init>(Ljava/lang/String;)V

    const-string p1, " "

    invoke-virtual {v0, p1}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    invoke-virtual {v0, p2}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    const-string p1, "\u0001"

    invoke-virtual {v0, p1}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    invoke-virtual {v0, p2}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    invoke-virtual {v0}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object p1

    invoke-interface {p0, p1}, Lkotlin/jvm/functions/Function1;->invoke(Ljava/lang/Object;)Ljava/lang/Object;

    .line 98
    return-void
.end method

.method public static schedule(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V
    .locals 2

    .line 68
    sget-object v0, Lapp/capri/ArenaScreen;->MAIN:Landroid/os/Handler;

    new-instance v1, Lapp/capri/ArenaScreen$1;

    invoke-direct {v1, p0, p1, p2}, Lapp/capri/ArenaScreen$1;-><init>(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V

    .line 70
    nop

    .line 68
    const-wide/16 p0, 0x384

    invoke-virtual {v0, v1, p0, p1}, Landroid/os/Handler;->postDelayed(Ljava/lang/Runnable;J)Z

    .line 71
    return-void
.end method

.method private static take(Ljava/lang/String;)Lapp/capri/ArenaScreen$Pending;
    .locals 2

    .line 245
    sget-object v0, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    monitor-enter v0

    .line 246
    :try_start_0
    invoke-static {}, Lapp/capri/ArenaScreen;->prune()V

    .line 247
    sget-object v1, Lapp/capri/ArenaScreen;->PENDING:Ljava/util/Map;

    invoke-interface {v1, p0}, Ljava/util/Map;->remove(Ljava/lang/Object;)Ljava/lang/Object;

    move-result-object p0

    check-cast p0, Lapp/capri/ArenaScreen$Pending;

    monitor-exit v0

    return-object p0

    .line 245
    :catchall_0
    move-exception p0

    monitor-exit v0
    :try_end_0
    .catchall {:try_start_0 .. :try_end_0} :catchall_0

    throw p0
.end method
