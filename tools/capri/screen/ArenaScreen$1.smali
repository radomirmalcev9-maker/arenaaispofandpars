.class Lapp/capri/ArenaScreen$1;
.super Ljava/lang/Object;
.source "ArenaScreen.java"

# interfaces
.implements Ljava/lang/Runnable;


# annotations
.annotation system Ldalvik/annotation/EnclosingMethod;
    value = Lapp/capri/ArenaScreen;->schedule(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V
.end annotation

.annotation system Ldalvik/annotation/InnerClass;
    accessFlags = 0x0
    name = null
.end annotation


# instance fields
.field private final synthetic val$done:Lkotlin/jvm/functions/Function1;

.field private final synthetic val$prefix:Ljava/lang/String;

.field private final synthetic val$watch:Lapp/capri/TypeWatch;


# direct methods
.method constructor <init>(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V
    .locals 0

    .line 68
    iput-object p1, p0, Lapp/capri/ArenaScreen$1;->val$watch:Lapp/capri/TypeWatch;

    iput-object p2, p0, Lapp/capri/ArenaScreen$1;->val$prefix:Ljava/lang/String;

    iput-object p3, p0, Lapp/capri/ArenaScreen$1;->val$done:Lkotlin/jvm/functions/Function1;

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method


# virtual methods
.method public run()V
    .locals 3

    .line 69
    iget-object v0, p0, Lapp/capri/ArenaScreen$1;->val$watch:Lapp/capri/TypeWatch;

    iget-object v1, p0, Lapp/capri/ArenaScreen$1;->val$prefix:Ljava/lang/String;

    iget-object v2, p0, Lapp/capri/ArenaScreen$1;->val$done:Lkotlin/jvm/functions/Function1;

    invoke-static {v0, v1, v2}, Lapp/capri/ArenaScreen;->access$6(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V

    return-void
.end method
