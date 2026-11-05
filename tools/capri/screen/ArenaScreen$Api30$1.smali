.class Lapp/capri/ArenaScreen$Api30$1;
.super Ljava/lang/Object;
.source "ArenaScreen.java"

# interfaces
.implements Ljava/lang/Runnable;


# annotations
.annotation system Ldalvik/annotation/EnclosingMethod;
    value = Lapp/capri/ArenaScreen$Api30;->request(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;Ljava/util/ArrayList;)V
.end annotation

.annotation system Ldalvik/annotation/InnerClass;
    accessFlags = 0x0
    name = null
.end annotation


# instance fields
.field private final synthetic val$capture:Lapp/capri/ArenaScreen$Api30$Capture;


# direct methods
.method constructor <init>(Lapp/capri/ArenaScreen$Api30$Capture;)V
    .locals 0

    .line 104
    iput-object p1, p0, Lapp/capri/ArenaScreen$Api30$1;->val$capture:Lapp/capri/ArenaScreen$Api30$Capture;

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method


# virtual methods
.method public run()V
    .locals 2

    .line 105
    iget-object v0, p0, Lapp/capri/ArenaScreen$Api30$1;->val$capture:Lapp/capri/ArenaScreen$Api30$Capture;

    const-string v1, "Screenshot timed out."

    invoke-virtual {v0, v1}, Lapp/capri/ArenaScreen$Api30$Capture;->fail(Ljava/lang/String;)V

    return-void
.end method
