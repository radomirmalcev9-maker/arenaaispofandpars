.class Lapp/capri/ArenaScreen$Api30$Capture$2;
.super Ljava/lang/Object;
.source "ArenaScreen.java"

# interfaces
.implements Ljava/lang/Runnable;


# annotations
.annotation system Ldalvik/annotation/EnclosingMethod;
    value = Lapp/capri/ArenaScreen$Api30$Capture;->fail(Ljava/lang/String;)V
.end annotation

.annotation system Ldalvik/annotation/InnerClass;
    accessFlags = 0x0
    name = null
.end annotation


# instance fields
.field final synthetic this$2:Lapp/capri/ArenaScreen$Api30$Capture;

.field private final synthetic val$message:Ljava/lang/String;


# direct methods
.method constructor <init>(Lapp/capri/ArenaScreen$Api30$Capture;Ljava/lang/String;)V
    .locals 0

    .line 177
    iput-object p1, p0, Lapp/capri/ArenaScreen$Api30$Capture$2;->this$2:Lapp/capri/ArenaScreen$Api30$Capture;

    iput-object p2, p0, Lapp/capri/ArenaScreen$Api30$Capture$2;->val$message:Ljava/lang/String;

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method


# virtual methods
.method public run()V
    .locals 3

    .line 178
    iget-object v0, p0, Lapp/capri/ArenaScreen$Api30$Capture$2;->this$2:Lapp/capri/ArenaScreen$Api30$Capture;

    invoke-static {v0}, Lapp/capri/ArenaScreen$Api30$Capture;->access$0(Lapp/capri/ArenaScreen$Api30$Capture;)Lkotlin/jvm/functions/Function1;

    move-result-object v0

    iget-object v1, p0, Lapp/capri/ArenaScreen$Api30$Capture$2;->this$2:Lapp/capri/ArenaScreen$Api30$Capture;

    invoke-static {v1}, Lapp/capri/ArenaScreen$Api30$Capture;->access$1(Lapp/capri/ArenaScreen$Api30$Capture;)Ljava/lang/String;

    move-result-object v1

    iget-object v2, p0, Lapp/capri/ArenaScreen$Api30$Capture$2;->val$message:Ljava/lang/String;

    invoke-static {v0, v1, v2}, Lapp/capri/ArenaScreen;->access$4(Lkotlin/jvm/functions/Function1;Ljava/lang/String;Ljava/lang/String;)V

    return-void
.end method
